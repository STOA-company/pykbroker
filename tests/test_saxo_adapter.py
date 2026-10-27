"""Saxo adapter unit tests on an injected fake transport (no network). The step-by-step walk against the
Saxo contract mock lives in quantus-trader tests/contract_walk/test_saxo_adapter_walk.py."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.saxo import (
    EXTERNAL_REFERENCE_MAX,
    SAXO_LIVE_URL,
    SAXO_SIM_URL,
    Saxo,


    _NoReply,
    _Reply,
    external_reference,
)
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, InvalidResponseError, NotSupportedError
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.reject_codes import DEFINITIVE_REJECTS, RATE_LIMITED, REJECTED, TOKEN_EXPIRED
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.rate_limit import RateLimiter
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

MAP = {"AAPL": (9001, "Stock", "US"), "EUR-USD": (9003, "FxSpot", "FX"), "D05": (9100, "Stock", "SG"),
       "AAPL.CFD": (9004, "CfdOnStock", "US")}


class Fake:
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def send(self, method, url, *, headers, body=None, form=None, timeout):
        self.sent.append((method, url, headers, body, form))
        item = self.replies.pop(0)
        if item is _NoReply:
            raise _NoReply
        return item


def safety():
    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="100000000", daily_krw_cap="200000000"),
        risk_ledger=MemoryRiskLedger(), idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda b, a: False),
        valuation=lambda r: Decimal(1000), market_order_allowed=lambda r: True)


def make(fake, mode="demo", **kw):
    t = [0.0]
    limiter = RateLimiter({"order": 1.0, "request": 2.0}, clock=lambda: t[0],
                          sleeper=lambda s: t.__setitem__(0, t[0] + s))
    base = {"account": "ACCT-KEY", "mode": mode, "app_key": "k", "app_secret": "s", "token": "t", "symbol_map": MAP,
            "client": fake, "limiter": limiter, "safety": safety()}
    base.update(kw)
    return Saxo(**base)


def order(**kw):
    base = dict(symbol="AAPL", side="buy", quantity=10, order_type="limit", price=Decimal("190.00"), market="US",
                client_key="ck-unit-1")
    base.update(kw)
    return OrderRequest(**base)


OK = _Reply(200, {"OrderId": "1"}, {})


# ---------------------------------------------------------------- models and capability defaults
def test_order_shape_validators():
    assert order(order_type="stop", price=None, stop_price=Decimal("180")).stop_price == Decimal("180")
    with pytest.raises(ValidationError):
        order(order_type="stop", price=None)                       # stop needs stop_price
    with pytest.raises(ValidationError):
        order(order_type="stop_limit", stop_price=None)
    with pytest.raises(ValidationError):
        order(stop_price=Decimal("1"))                             # limit with stop_price
    with pytest.raises(ValidationError):
        order(time_in_force="gtd")                                 # gtd needs expire_at
    with pytest.raises(ValidationError):
        order(time_in_force="gtd", expire_at=datetime(2026, 11, 1))  # naive
    with pytest.raises(ValidationError):
        AmendRequest(order_id="1", symbol="AAPL", side="buy", quantity=1, order_type="market", market="US",
                     client_key="a")
    assert order(market="SG", symbol="D05").market == "SG"


@pytest.mark.parametrize("cls", [KIS, DBFI])
def test_existing_brokers_refuse_new_shapes_and_amend(cls):
    broker = cls.__new__(cls)
    broker.market, broker.virtual = "KR", False
    caps = broker.capabilities()
    assert (caps.supports_stop_order, caps.supports_stop_limit_order, caps.supports_amend) == (False, False, False)
    assert caps.time_in_force == () and caps.asset_types == ()


# ---------------------------------------------------------------- reject table + external reference
def test_reject_table_rows_have_sources_and_unknowns_are_absent():
    table = DEFINITIVE_REJECTS["saxo"]
    assert all(r.source.startswith("https://www.developer.saxo/") for r in table.values())
    assert {r.kind for r in table.values()} == {REJECTED, TOKEN_EXPIRED, RATE_LIMITED}
    for unknown in ("OrderCommandPending", "OrderCommandTimeout", "CouldNotCompleteRequest", "OtherError",
                    "BrokerOption", "AlreadyPendingCancelReplace", "TradeNotCompleted", "HTTP409", "HTTP503"):
        assert unknown not in table
    assert table["InsufficientCash"].kind == REJECTED and table["HTTP401"].kind == TOKEN_EXPIRED


def test_external_reference_rule():
    assert external_reference("q-real-abc") == "q-real-abc"
    long = "q-real-" + "x" * 100
    ref = external_reference(long)
    assert len(ref) <= EXTERNAL_REFERENCE_MAX and ref.startswith("ck-") and ref == external_reference(long)
    # a short key that imitates a hashed form is hashed too: namespaces never meet
    assert external_reference("ck-abc") != "ck-abc"
    assert external_reference("ck-abc").startswith("ck-") and len(external_reference("ck-abc")) == 43


def test_external_reference_no_collision_over_many_keys():
    keys = [f"q-real-{i:032x}-d{i % 7}-s{i % 11}" + ("z" * (i % 90)) for i in range(20000)]
    refs = {external_reference(k) for k in keys}
    assert len(refs) == len(set(keys)) and all(len(r) <= EXTERNAL_REFERENCE_MAX for r in refs)
    long_a, long_b = "a" * 128, "a" * 127 + "b"
    assert external_reference(long_a) != external_reference(long_b)


# ---------------------------------------------------------------- bodies for every order shape
@pytest.mark.parametrize("kw,expect", [
    (dict(order_type="market", price=None), {"OrderType": "Market"}),
    (dict(), {"OrderType": "Limit", "OrderPrice": 190.0}),
    (dict(order_type="stop", price=None, stop_price=Decimal("185")), {"OrderType": "Stop", "OrderPrice": 185.0}),
    (dict(order_type="stop_limit", stop_price=Decimal("185")),
     {"OrderType": "StopLimit", "OrderPrice": 190.0, "StopLimitPrice": 185.0}),
])
def test_order_type_bodies(kw, expect):
    fake = Fake(OK)
    make(fake).place_order(order(**kw))
    body = fake.sent[0][3]
    assert {k: body[k] for k in expect} == expect
    assert body["OrderDuration"] == {"DurationType": "DayOrder"} and body["AccountKey"] == "ACCT-KEY"
    assert ("OrderPrice" in body) == ("OrderPrice" in expect)


@pytest.mark.parametrize("tif,name", [("day", "DayOrder"), ("gtc", "GoodTillCancel"),
                                      ("ioc", "ImmediateOrCancel"), ("fok", "FillOrKill")])
def test_duration_bodies(tif, name):
    fake = Fake(OK)
    make(fake).place_order(order(time_in_force=tif))
    assert fake.sent[0][3]["OrderDuration"] == {"DurationType": name}


def test_gtd_carries_utc_expiry():
    fake = Fake(OK)
    make(fake).place_order(order(time_in_force="gtd", expire_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC)))
    assert fake.sent[0][3]["OrderDuration"] == {"DurationType": "GoodTillDate",
                                                "ExpirationDateTime": "2026-11-02T09:00:00Z"}


def test_assets_and_markets_open_and_closed():
    fake = Fake(OK, OK, OK)
    broker = make(fake)
    broker.place_order(order(client_key="a1"))
    broker.place_order(order(symbol="EUR-USD", market="FX", quantity=1000, client_key="a2"))
    broker.place_order(order(symbol="D05", market="SG", client_key="a3"))
    assert [s[3]["AssetType"] for s in fake.sent] == ["Stock", "FxSpot", "Stock"]
    caps = broker.capabilities()
    assert caps.asset_types == ("Stock", "Etf", "FxSpot")
    assert set(caps.asset_types_closed) == {"CfdOnStock", "CfdOnIndex", "ContractFutures"}
    # CFD: the capability column is closed, the refusal happens before any HTTP
    with pytest.raises(NotSupportedError):
        broker.place_order(order(symbol="AAPL.CFD", client_key="a4"))
    assert len(fake.sent) == 3
    # same interface, opened by data only
    fake2 = Fake(OK)
    make(fake2, asset_types=("Stock", "Etf", "FxSpot", "CfdOnStock")).place_order(order(symbol="AAPL.CFD", client_key="a5"))
    assert fake2.sent[0][3]["AssetType"] == "CfdOnStock"


def test_undeclared_market_and_unmapped_symbol_refused_before_http():
    fake = Fake()
    broker = make(fake, markets=("US",))
    with pytest.raises(NotSupportedError):
        broker.place_order(order(symbol="D05", market="SG"))
    with pytest.raises(NotSupportedError):
        broker.place_order(order(symbol="NOPE"))
    assert fake.sent == []


# ---------------------------------------------------------------- SEAM §4 classes
def test_failure_classes():
    cases = [
        (_Reply(401, None, {}), BrokerRejectedError, TOKEN_EXPIRED),
        (_Reply(429, None, {}), BrokerRejectedError, RATE_LIMITED),
        (_Reply(400, {"ErrorInfo": {"ErrorCode": "InsufficientCash"}}, {}), BrokerRejectedError, REJECTED),
        (_Reply(200, {"ErrorInfo": {"ErrorCode": "MarketClosed"}}, {}), BrokerRejectedError, REJECTED),
        (_Reply(409, None, {}), InvalidResponseError, None),
        (_Reply(503, None, {}), InvalidResponseError, None),
        (_Reply(202, {"ErrorInfo": {"ErrorCode": "TradeNotCompleted"}, "OrderId": "9"}, {}), InvalidResponseError, None),
        (_Reply(400, {"ErrorInfo": {"ErrorCode": "OrderCommandTimeout"}}, {}), InvalidResponseError, None),
        (_Reply(400, {"ErrorInfo": {"ErrorCode": "BrandNewCode"}}, {}), InvalidResponseError, None),
        (_Reply(200, {"OrderId": "1", "ExternalReference": "different"}, {}), InvalidResponseError, None),
        (_Reply(200, {}, {}), InvalidResponseError, None),
    ]
    for reply, exc, kind in cases:
        fake = Fake(reply, reply)  # a second reply must never be used: no resend
        with pytest.raises(exc) as info:
            make(fake).place_order(order(client_key=f"c-{reply.status}-{id(reply)}"))
        assert len(fake.sent) == 1, reply
        if kind:
            assert info.value.kind == kind


def test_lost_response_is_unknown_not_sent_twice():
    fake = Fake(_NoReply, OK)
    with pytest.raises(Exception) as info:
        make(fake).place_order(order())
    assert type(info.value).__name__ == "TransportError" and len(fake.sent) == 1


# ---------------------------------------------------------------- mode / gateway
def test_gateway_follows_mode_only():
    assert make(Fake(), "demo")._base == SAXO_SIM_URL
    assert make(Fake(), "live")._base == SAXO_LIVE_URL
    with pytest.raises(ConfigurationError):
        make(Fake(), "live", base_url="http://127.0.0.1:1")        # loopback is demo-only
    with pytest.raises(ConfigurationError):
        make(Fake(), "demo", base_url="https://evil.example.com")
    with pytest.raises(ConfigurationError):
        make(Fake(), None)
    assert make(Fake(), "live").virtual is False and make(Fake(), "demo").virtual is True


def test_paper_never_calls_saxo():
    fake = Fake()
    broker = make(fake, "paper", token=None)
    placed = broker.place_order(order())
    assert placed.virtual is True and placed.status == "accepted"
    assert broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=10, market="US",
                                             client_key="cc-1")).status == "cancelled"
    with pytest.raises(NotSupportedError):
        broker.list_orders()
    with pytest.raises(NotSupportedError):
        broker.get_balance()
    assert fake.sent == []


def test_amend_is_optional_on_base():
    from pykbroker.base import AmendableBroker

    assert isinstance(make(Fake()), AmendableBroker)
    for cls in (KIS, DBFI):
        assert not hasattr(cls, "amend_order")  # optional: existing adapters are untouched


# ---------------------------------------------------------------- cancel / status close only on the order activity
ACK = _Reply(200, {"Orders": [{"OrderId": "1"}]}, {})
EMPTY = _Reply(200, {"Data": []}, {})


def activity(status, sub="Confirmed", **kw):
    row = {"OrderId": "1", "Uic": 9001, "AssetType": "Stock", "BuySell": "Buy", "Amount": 10, "Status": status,
           "SubStatus": sub, "LogId": "7"}
    row.update(kw)
    return _Reply(200, {"Data": [row]}, {})


def cancel(fake):
    return make(fake).cancel_order(CancelRequest(order_id="1", symbol="AAPL", quantity=10, market="US",
                                                 client_key="cc-unit"))


@pytest.mark.parametrize("last, expect", [
    # A fill that raced the acknowledged cancel: absent from the order list, the activity says FinalFill.
    (activity("FinalFill", FilledAmount=10, AveragePrice=190.5), ("filled", 10, 0, Decimal("190.5"))),
    # Partial fill then cancel: the 3 filled shares stay on the result.
    (activity("Cancelled", FilledAmount=3, AveragePrice=190.0), ("cancelled", 3, 7, Decimal("190.0"))),
    (activity("Cancelled", FilledAmount=0), ("cancelled", 0, 10, None)),
    (activity("Expired", FilledAmount=0), ("cancelled", 0, 10, None)),
])
def test_cancel_outcome_comes_from_the_last_order_activity(last, expect):
    fake = Fake(ACK, EMPTY, last)
    out = cancel(fake)
    assert (out.status, out.filled, out.remaining, out.fill_price) == expect and out.id == "1"
    path = fake.sent[2][1]
    assert "/cs/v1/audit/orderactivities?" in path and "OrderId=1" in path and "EntryType=Last" in path
    assert [m for m, *_ in fake.sent].count("DELETE") == 1


@pytest.mark.parametrize("last", [
    EMPTY,                                               # not logged yet
    activity("Cancelled", sub="Requested", FilledAmount=0),  # cancel asked, not confirmed
    activity("Cancelled"),                               # no FilledAmount: a partial fill could hide
    activity("Fill", FilledAmount=4),                    # not final
    activity("FinalFill", FilledAmount=4),               # final fill that does not fill the order: inconsistent
    activity("Cancelled", FilledAmount=11),              # fills more than it holds
    _Reply(503, None, {}),                               # read failed
])
def test_cancel_absent_from_list_without_a_final_activity_is_unknown(last):
    out = cancel(Fake(ACK, EMPTY, last))
    assert out.status == "unknown" and out.filled is None


def test_cancel_still_listed_is_unknown_and_keeps_the_filled_part():
    working = _Reply(200, {"Data": [{"OrderId": "1", "Uic": 9001, "AssetType": "Stock", "Amount": 10,
                                     "FilledAmount": 3, "Status": "WorkingLockedCancelPending"}]}, {})
    fake = Fake(ACK, working)
    out = cancel(fake)
    assert (out.status, out.filled, out.remaining) == ("unknown", 3, 7) and len(fake.sent) == 2


def test_status_of_an_order_gone_from_the_lists_reads_its_activity():
    from pykbroker.models import Order

    placed = Order(id="1", symbol="AAPL", market="US", side="buy", order_type="limit", quantity=10,
                   price=Decimal("190"), status="accepted", external_reference="ck-unit-1")
    done = make(Fake(EMPTY, EMPTY, activity("FinalFill", FilledAmount=10, AveragePrice=189.9))).status(placed)
    assert (done.status, done.filled, done.remaining, done.fill_price) == ("filled", 10, 0, Decimal("189.9"))
    assert (done.order_type, done.price, done.external_reference) == ("limit", Decimal("190"), "ck-unit-1")
    assert make(Fake(EMPTY, EMPTY, EMPTY)).status(placed).status == "unknown"


# ---------------------------------------------------------------- token refresh (rotating refresh token)
def refreshed(access="a1", refresh="r1", life=1200):
    body = {"access_token": access, "expires_in": life}
    if refresh is not None:
        body["refresh_token"] = refresh
    return _Reply(200, body, {})


def test_refresh_without_a_new_refresh_token_keeps_the_current_one():
    seen = []
    broker = make(Fake(refreshed(refresh=None)), refresh_token="r0", on_token_refresh=seen.append)
    broker._refresh()
    assert broker._refresh_token == "r0" and seen[-1].refresh_token == "r0" and seen[-1].token == "a1"


def test_refresh_refused_retries_once_with_the_owners_newer_refresh_token():
    fake = Fake(_Reply(400, {"error": "invalid_grant"}, {}), refreshed(refresh="r2"))
    seen = []
    broker = make(fake, refresh_token="r0", on_token_refresh=seen.append, reload_refresh_token=lambda: "r1")
    broker._refresh()
    assert [s[4]["refresh_token"] for s in fake.sent] == ["r0", "r1"]
    assert broker._refresh_token == "r2" and seen[-1].refresh_token == "r2"


@pytest.mark.parametrize("reload", [None, lambda: "r0", lambda: None, lambda: "", lambda: 1 / 0])
def test_refresh_refused_without_a_different_stored_token_is_not_retried(reload):
    fake = Fake(_Reply(400, {"error": "invalid_grant"}, {}), refreshed())
    broker = make(fake, refresh_token="r0", reload_refresh_token=reload)
    with pytest.raises(BrokerRejectedError):
        broker._refresh()
    assert len(fake.sent) == 1


def test_refresh_retry_happens_at_most_once():
    fake = Fake(_Reply(400, {}, {}), _Reply(400, {}, {}), refreshed())
    tokens = iter(["r1", "r2"])
    broker = make(fake, refresh_token="r0", reload_refresh_token=lambda: next(tokens))
    with pytest.raises(BrokerRejectedError):
        broker._refresh()
    assert [s[4]["refresh_token"] for s in fake.sent] == ["r0", "r1"]
