"""Official LS field contract + fabricated state, through the native HTTP path.

No spec switch or account-mode check is patched. conftest denies real sockets.
This establishes offline mapping coverage, not LS sandbox/live certification.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from pykbroker.brokers import ls as module
from pykbroker.brokers.ls import LS
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DryRunError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter
from tests.ls_contract_server import ContractServer, contract


def order(**changes):
    return OrderRequest(**({"symbol": "005930", "side": "buy", "quantity": 3,
                           "price": "10000", "client_key": "fixture-place"} | changes))


def cancel(order_id, **changes):
    return CancelRequest(**({"order_id": order_id, "symbol": "005930", "quantity": 1,
                            "client_key": "fixture-cancel"} | changes))


@pytest.fixture
def native(monkeypatch, live_safety):
    server = ContractServer()
    monkeypatch.setattr(module, "urlopen", server.urlopen)
    live_safety.valuation = lambda request: Decimal("30000")
    live_safety.market_order_allowed = lambda request: True
    options = LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)

    def build(*, stored=None, safety=live_safety, enabled=True, limiter=None):
        return LS(account="fixture-account", client_options=options if stored is None else stored,
                  allow_live_orders=enabled, safety=safety, clock=server.clock,
                  rate_limiter=limiter if limiter is not None else Mock(spec=RateLimiter))

    return server, build(), build, options


@pytest.mark.parametrize("tr", ["CSPAT00601", "CSPAT00701", "CSPAT00801", "CSPAQ12200", "t0424", "t0425"])
def test_current_official_rates_and_paths(tr):
    doc = contract(tr)
    assert doc["version"] == "20261022083305082"
    assert module.LS_RATES[tr] == int(doc["transaction"]["transactionPerSec"])
    assert doc["path"] == ("/stock/order" if tr.startswith("CSPAT") else "/stock/accno")


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind,price", [("limit", "10000"), ("market", None)])
def test_native_market_limit_buy_sell_and_complete_fill(native, side, kind, price):
    server, broker, _, _ = native
    request = order(side=side, order_type=kind, price=price)
    placed = broker.place_order(request)
    assert placed.status == "accepted" and placed.virtual is False
    body = next(call[3]["CSPAT00601InBlock1"] for call in server.calls if call[1] == "CSPAT00601")
    assert body["BnsTpCode"] == ("2" if side == "buy" else "1")
    assert body["OrdprcPtnCode"] == ("00" if kind == "limit" else "03")
    assert body["OrdPrc"] == (10000 if kind == "limit" else 0)
    assert broker.status(placed).status == "open"
    server.fill(placed.id, 3)
    result = broker.status(placed)
    assert (result.quantity, result.filled, result.remaining, result.status) == (3, 3, 0, "filled")
    assert result.fill_price is None  # cheprice is a fill price, not documented as an average.
    before = sum(call[1] == "CSPAT00601" for call in server.calls)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert sum(call[1] == "CSPAT00601" for call in server.calls) == before == 1
    assert module.LS_SPEC_VERIFIED is False


def test_partial_fill_modify_cancel_and_account_queries(native):
    server, broker, _, _ = native
    placed = broker.place_order(order())
    server.fill(placed.id, 1)
    partial = broker.status(placed)
    assert (partial.filled, partial.remaining, partial.status) == (1, 2, "partial")
    amendment = order(quantity=2, price="9900", client_key="fixture-modify")
    amended = broker.modify_order(placed.id, amendment)
    assert amended.client_key == amendment.client_key
    assert amended.id != placed.id and amended.status == "accepted"
    body = next(call[3]["CSPAT00701InBlock1"] for call in server.calls if call[1] == "CSPAT00701")
    assert body == {"OrgOrdNo": int(placed.id), "IsuNo": "005930", "OrdQty": 2,
                    "OrdprcPtnCode": "00", "OrdCndiTpCode": "0", "OrdPrc": 9900}
    assert broker.status(amended).remaining == 2
    request = cancel(amended.id, quantity=2)
    result = broker.cancel_order(request)
    assert result.id == amended.id and result.status == "accepted"
    snapshot = broker.status(amended)
    assert snapshot.remaining == 0 and snapshot.status == "unknown"
    assert broker.status(placed).filled == 1  # Amendment never erases the original fill.
    balance = broker.get_balance()
    assert (balance.cash_available, balance.position_value) == (Decimal(900000 - 9950), Decimal(40000))
    position, = broker.get_positions()
    assert (position.symbol, position.quantity, position.sellable) == ("005930", 4, 4)
    assert position.average_price == Decimal("9912.5")
    for action in (lambda: broker.modify_order(placed.id, amendment), lambda: broker.cancel_order(request)):
        with pytest.raises(DuplicateOrderError):
            action()
    assert sum(call[1] == "CSPAT00701" for call in server.calls) == 1
    assert sum(call[1] == "CSPAT00801" for call in server.calls) == 1


def test_amendment_limit_and_market_is_refused_before_post(native):
    server, broker, _, _ = native
    placed = broker.place_order(order())
    amended = broker.modify_order(placed.id, order(order_type="limit", price="9900", client_key="amend"))
    assert amended.id in server.orders and amended.order_type == "limit"
    amends = sum(call[1] == "CSPAT00701" for call in server.calls)
    # The unified amend shape changes a limit price only; the API accepts 03 but a market amend would drop
    # the price the safety valuation reserved. Cancel, confirm, then place anew (docs/ls-spec.md).
    with pytest.raises(NotSupportedError):
        broker.modify_order(placed.id, order(order_type="market", price=None, client_key="amend-m"))
    assert sum(call[1] == "CSPAT00701" for call in server.calls) == amends


def test_amendment_does_not_echo_unverified_requested_side(native):
    server, broker, _, _ = native
    broker.place_order(order())
    amended = broker.modify_order(
        "101", order(side="sell", client_key="amend-side-mismatch")
    )
    assert amended.side is None
    amendment = next(call[3]["CSPAT00701InBlock1"] for call in server.calls if call[1] == "CSPAT00701")
    assert "BnsTpCode" not in amendment


@pytest.mark.parametrize("ttl_field", ["expire_in", "expires_in"])
def test_token_issue_expiry_and_explicit_reissue_before_write(native, ttl_field):
    server, _, build, options = native
    assert set(options) == {"token", "token_type", "expires_at"}
    server.advance(120)
    expired = build()
    calls = len(server.calls)
    with pytest.raises(ConfigurationError, match="expired"):
        expired.place_order(order())
    with pytest.raises(ConfigurationError, match="expired"):
        expired.get_balance()
    assert len(server.calls) == calls  # No expired token is sent, no order key is claimed.
    server.ttl_field = ttl_field
    renewed = LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)
    fresh = build(stored=renewed)
    result = fresh.place_order(order())
    assert result.status == "accepted" and len(server.orders) == 1
    assert len(server.tokens) == 2
    assert sum(call[1] == "token" for call in server.calls) == 2


def test_token_expiry_during_write_rate_wait_blocks_transport_and_keeps_claim(native):
    server, _, build, _ = native
    server.token_ttl = 1
    options = LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)
    limiter = Mock(spec=RateLimiter)

    def wait(tr):
        if tr == "CSPAT00601":
            server.advance(2)

    limiter.wait.side_effect = wait
    broker = build(stored=options, limiter=limiter)
    request = order(client_key="expiry-during-wait")
    with pytest.raises(ConfigurationError, match="expired"):
        broker.place_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.safety.execute(broker.broker_code, broker._account, request, lambda: None)
    assert not any(call[1] == "CSPAT00601" for call in server.calls)
    assert sum(call[1] == "token" for call in server.calls) == 2


@pytest.mark.parametrize("bad", [
    {}, {"expire_in": "0"}, {"expire_in": True}, {"expire_in": "NaN"},
    {"expire_in": "120", "expires_in": "121"}, {"expire_in": "120", "token_type": "unknown"},
    {"expire_in": "120", "access_token": "bad\r\ntoken"},
])
def test_invalid_token_response_does_not_escape(native, bad):
    server, _, _, _ = native
    server.next_response = {"access_token": "fixture-token", "token_type": "Bearer", **bad}
    with pytest.raises(InvalidResponseError):
        LS.issue_token(app_key="fixture-app-key", app_secret="fixture-app-secret", clock=server.clock)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_synthetic_http_error_never_replays_a_write(native, status):
    server, broker, _, _ = native
    # Trigger after the account identity read so the failed HTTP request is the write.
    broker._verify_account()
    original = server.urlopen

    def fail_write(request, *, timeout):
        if dict((k.lower(), v) for k, v in request.header_items()).get("tr_cd") == "CSPAT00601":
            server.next_http_status = status
        return original(request, timeout=timeout)

    module.urlopen = fail_write
    request = order()
    with pytest.raises(TransportError):
        broker.place_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request)
    assert sum(call[1] == "CSPAT00601" for call in server.calls) == 1
    assert len(server.tokens) == 1


@pytest.mark.parametrize("code", ["SYNTH_AUTH", "SYNTH_RATE", "SYNTH_ORDER"])
def test_three_synthetic_nonzero_codes_reject_reads_and_preserve_write_uncertainty(native, code):
    server, broker, _, _ = native
    response = {"rsp_cd": code, "rsp_msg": "fabricated", "CSPAQ12200OutBlock2": {"MnyOrdAbleAmt": 100}}
    server.next_response = response
    with pytest.raises(BrokerRejectedError):
        broker.get_balance()
    # Public docs do not define meanings for these codes. Even OrdNo cannot turn one into acceptance.
    original = server.urlopen

    def reject_write(request, *, timeout):
        if dict((k.lower(), v) for k, v in request.header_items()).get("tr_cd") == "CSPAT00601":
            server.next_response = {"rsp_cd": code, "CSPAT00601OutBlock2": {"OrdNo": 999}}
        return original(request, timeout=timeout)

    module.urlopen = reject_write
    with pytest.raises(InvalidResponseError, match="reconcile"):
        broker.place_order(order())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(order())
    assert sum(call[1] == "CSPAT00601" for call in server.calls) == 1


def test_accepted_order_then_transport_loss_keeps_key_and_reconciles(native):
    server, broker, _, _ = native
    server.after_write_failure = True
    with pytest.raises(TransportError):
        broker.place_order(order())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(order())
    result, = broker.list_orders()
    assert result.id == "101" and result.status == "open"
    assert len(server.orders) == 1 and len(server.tokens) == 1


def test_official_rate_budget_waits_and_server_throttle_surfaces(native):
    server, _, build, _ = native
    server.enforce_rates = True
    limiter = RateLimiter(module.LS_RATES, clock=server.monotonic, sleeper=server.advance)
    broker = build(limiter=limiter)
    start = server.now
    for _ in range(3):
        broker.get_balance()
    assert server.now - start == timedelta(seconds=2)
    # A distinct uncoordinated caller exceeds the documented 1/s account TR.
    uncoordinated = build()
    with pytest.raises(BrokerRejectedError):
        uncoordinated.get_balance()


def test_default_live_gate_dry_run_and_closed_client_block_amendment(native):
    server, broker, build, _ = native
    with pytest.raises(SpecUnverifiedError):
        build(enabled=False).modify_order("101", order())
    # Existing DryRun owns the guard; use an unmodified default safety instance.
    from pykbroker.safety.gate import OrderSafety

    with pytest.raises(DryRunError):
        build(safety=OrderSafety()).modify_order("101", order())
    broker.close()
    with pytest.raises(ConfigurationError, match="closed"):
        broker.modify_order("101", order())
    assert not server.orders and module.LS_SPEC_VERIFIED is False


def test_amendment_reuses_place_namespace_and_revalidates(native):
    server, broker, _, _ = native
    placed = broker.place_order(order())
    with pytest.raises(DuplicateOrderError):
        broker.modify_order(placed.id, order())
    for number in ("0", "-1", True, "not-a-number"):
        with pytest.raises(ConfigurationError):
            broker.modify_order(number, order(client_key="amend"))
    broker.market = "US"
    with pytest.raises(NotSupportedError):
        broker.modify_order(placed.id, order(client_key="amend"))
    assert sum(call[1] == "CSPAT00701" for call in server.calls) == 0
