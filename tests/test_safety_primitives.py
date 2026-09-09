from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pykbroker.exceptions import DryRunError, KillSwitchError, RiskCapError, SafetyError
from pykbroker.models import OrderRequest
from pykbroker.safety.circuit_breaker import evaluate_breaker
from pykbroker.safety.dry_run import DryRun, mask_account, redact_order_for_log, should_execute_live
from pykbroker.safety.idempotency import MemoryIdempotencyStore, order_key
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter
from pykbroker.safety.risk_caps import RiskLimits, check_caps
from pykbroker.safety.storage import SQLiteLedger


@pytest.mark.parametrize("value", [None, True, "false", "", 0, 1, [], {}])
def test_dry_run_fail_closed(value):
    assert not should_execute_live({"dry_run": value})
    with pytest.raises(DryRunError):
        DryRun(value).require_live()


def test_dry_run_default_constructor_is_fail_closed():
    """DryRun() with no explicit argument must still block live operations."""
    with pytest.raises(DryRunError):
        DryRun().require_live()


def test_explicit_false_is_required():
    assert not should_execute_live()
    assert not should_execute_live({})
    assert should_execute_live({"dry_run": False})
    DryRun(False).require_live()


@pytest.mark.parametrize("value", [None, True, 0, "false"])
def test_kill_switch_missing_and_ambiguous(value):
    with pytest.raises(KillSwitchError):
        KillSwitch(lambda broker, account: value).require_open("test", "fixture")
    with pytest.raises(KillSwitchError):
        KillSwitch().require_open("test", "fixture")


def test_kill_switch_error_does_not_leak():
    callback = Mock(side_effect=ValueError("sensitive"))
    with pytest.raises(KillSwitchError, match="unavailable") as error:
        KillSwitch(callback).require_open("test", "fixture")
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("qty", [0, -1, 1.5, True, "2"])
def test_invalid_quantities(qty):
    with pytest.raises(ValidationError):
        OrderRequest(symbol="005930", side="buy", quantity=qty, price="1", client_key="x")


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", "0"])
def test_invalid_prices(price):
    with pytest.raises(ValidationError):
        OrderRequest(symbol="005930", side="buy", quantity=1, price=price, client_key="x")


def test_idempotency_canonical_numbers_and_unambiguous_encoding():
    kwargs = dict(account="fixture", broker="kis", symbol="005930", side="buy", cycle="cycle")
    assert order_key(**kwargs, quantity="1.00", price=Decimal("2.0")) == order_key(**kwargs, quantity=1, price="2")
    assert order_key(**kwargs, quantity=1, price="2") != order_key(**kwargs, quantity=1, price="3")
    assert order_key(**{**kwargs, "account": "a\u001fb", "broker": "c"}, quantity=1) != order_key(
        **{**kwargs, "account": "a", "broker": "b\u001fc"}, quantity=1
    )


def test_memory_claim_concurrent_once():
    ledger = MemoryIdempotencyStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda _: ledger.claim("scope", "same-key"), range(40)))
    assert sum(admitted) == 1
    assert ledger.claim("other-account", "same-key")


def test_sqlite_survives_new_instance_and_concurrent_claims(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    assert SQLiteLedger(path).claim("scope", "key")
    assert not SQLiteLedger(path).claim("scope", "key")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: SQLiteLedger(path).claim("scope", "race"), range(10)))
    assert sum(results) == 1


def test_caps_are_gross_and_atomic(tmp_path):
    limits = RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="60", daily_krw_cap="100")
    path = tmp_path / "ledger.sqlite3"

    def reserve(_):
        try:
            SQLiteLedger(path).reserve("scope", date(2026, 9, 9), Decimal(60), limits)
            return True
        except RiskCapError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1
    SQLiteLedger(path).reserve("scope", date(2026, 9, 10), Decimal(60), limits)


@pytest.mark.parametrize("amount,spent", [("101", "0"), ("60", "150"), ("NaN", "0"), ("1", "Infinity"), ("-1", "0")])
def test_bad_caps_fail(live_safety, amount, spent):
    # Small limits belong to this test, never package defaults.
    caps = RiskLimits(allow_orders=True, per_order_krw_cap="100", daily_krw_cap="200")
    with pytest.raises(RiskCapError):
        check_caps(Decimal(amount), Decimal(spent), caps)


def test_missing_caps_fail():
    with pytest.raises(RiskCapError):
        check_caps(Decimal(1), Decimal(0), None)


def test_pipeline_order_and_duplicate(live_safety, request_order):
    events = []

    class Risk:
        def reserve(self, *args):
            events.append("caps")

    class Keys:
        def claim(self, *args):
            events.append("key")
            return True

    class Dry:
        def require_live(self):
            events.append("dry_run")

    live_safety.dry_run = Dry()
    live_safety.risk_ledger, live_safety.idempotency = Risk(), Keys()
    live_safety.execute("fixture", "account", request_order, lambda: events.append("adapter"))
    assert events == ["dry_run", "caps", "key", "adapter"]


def test_missing_usage_and_key_cannot_execute(request_order, live_safety):
    for attribute in ("risk_ledger", "idempotency"):
        previous = getattr(live_safety, attribute)
        setattr(live_safety, attribute, None)
        submit = Mock()
        with pytest.raises(SafetyError):
            live_safety.execute("test", "account", request_order, submit)
        submit.assert_not_called()
        setattr(live_safety, attribute, previous)


def test_market_has_no_trusted_valuation(live_safety):
    order = OrderRequest(symbol="005930", side="buy", quantity=1, order_type="market", client_key="market")
    with pytest.raises(RiskCapError):
        live_safety.execute("test", "account", order, lambda: pytest.fail("submitted"))


def test_day_uses_kst(live_safety, request_order):
    ledger = Mock()
    live_safety.risk_ledger = ledger
    live_safety.clock = lambda: datetime(2026, 9, 9, 16, tzinfo=UTC)
    live_safety.execute("test", "account", request_order, lambda: None)
    assert ledger.reserve.call_args.args[1] == date(2026, 9, 10)


def test_redaction_is_allowlisted():
    payload = {"account": "fixture-1234", "api_key": "fake-sensitive", "payload": {"x": "private"}, "action": "place"}
    cleaned = redact_order_for_log(payload)
    assert cleaned == {"account": "****1234", "api_key": "****", "payload": "****", "action": "place"}
    assert payload["account"] == "fixture-1234"
    assert mask_account("1234") == "****"


@pytest.mark.parametrize("metrics", [None, {}, {"realized_pnl_pct": "NaN"}])
def test_breaker_missing_metrics_trips(metrics):
    assert evaluate_breaker(metrics, {}) == {"trip": True, "reasons": ["unparseable"]}


def test_breaker_thresholds_and_counts():
    metrics = dict(realized_pnl_pct=-3, error_rate=0.5, order_count=10, consecutive_errors=4)
    caps = dict(max_loss_pct=3, max_error_rate=0.5, max_order_count=10, max_consecutive_errors=4)
    assert evaluate_breaker(metrics, caps)["reasons"] == [
        "daily_loss",
        "error_rate",
        "order_count",
        "consecutive_errors",
    ]
    metrics["order_count"] = 1.5
    assert evaluate_breaker(metrics, caps)["reasons"] == ["unparseable"]


def test_shared_limiter_with_injected_clock():
    times, waits = [0.0], []

    def sleep(delay):
        waits.append(delay)
        times[0] += delay

    a = credential_rate_limiter("test", "fixture", {"quote": 1}, clock=lambda: times[0], sleeper=sleep)
    b = credential_rate_limiter("test", "fixture", {"quote": 2}, clock=lambda: times[0], sleeper=sleep)
    assert a is b
    a.wait("quote")
    b.wait("quote")
    a.wait("quote")
    assert waits == [1, 1]
    with pytest.raises(Exception, match="supplied"):
        RateLimiter().wait("unknown")
