"""Official-contract walks only: this is not a live/sandbox execution claim."""

from datetime import date
from decimal import Decimal
from io import BytesIO
from urllib.error import HTTPError

import pytest

from pykbroker.brokers.meritz import Meritz, _MeritzRestClient
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

from .meritz_contract_server import SPEC, ContractServer, validate


@pytest.fixture
def world(live_safety, monkeypatch):
    monkeypatch.setattr(RateLimiter, "wait", lambda *args: None)
    live_safety.market_order_allowed = lambda request: True
    live_safety.valuation = lambda request: Decimal(1000)
    def make(market="KR", venue=None):
        server = ContractServer()
        auth = Meritz.issue_token(app_key="fixture-key", secret_key="fixture-secret", client_factory=lambda: server)
        broker = Meritz(account="fixture-account", bound_account="fixture-account", app_key="fixture-key",
                        token=auth["access_token"], mac_address="001122AABBCC", client=server,
                        market=market, venue=venue, order_date=date(2026, 10, 25), writes_enabled=True,
                        safety=live_safety)
        return server, broker
    return make


def order(market="KR", **kw):
    return OrderRequest(**({"symbol": "005930" if market == "KR" else "BRK.B", "market": market,
                           "side": "buy", "quantity": 3, "price": "1000", "client_key": "place",
                           "venue": "KRX" if market == "KR" else "NYSE"} | kw))


def amend(placed, **kw):
    return AmendRequest(**({"order_id": placed.id, "symbol": placed.symbol, "market": placed.market,
                           "side": "buy", "quantity": 2, "price": "1100", "client_key": "amend",
                           "venue": "KRX" if placed.market == "KR" else "NYSE"} | kw))


def cancel(placed, **kw):
    return CancelRequest(**({"order_id": placed.id, "symbol": placed.symbol, "market": placed.market,
                            "quantity": 2, "client_key": "cancel",
                            "venue": "KRX" if placed.market == "KR" else "NYSE"} | kw))


@pytest.mark.parametrize("market,venue", [("KR", None), ("US", "NYSE")])
def test_order_partial_amend_cancel_balance_and_fill_walk(world, market, venue):
    server, broker = world(market, venue)
    placed = broker.place_order(order(market))
    assert placed.status == "accepted" and placed.client_key == "place"
    [opened] = broker.list_orders()
    assert (opened.symbol, opened.remaining, opened.status) == (placed.symbol, 3, "open")
    server.fill(market, placed.id, 1, 990)
    partial = broker.status(placed)
    assert (partial.status, partial.filled, partial.remaining, partial.fill_price) == ("partial", 1, 2, Decimal(990))
    assert partial.client_key == "place"
    amended = broker.amend_order(amend(placed))
    assert amended.id != placed.id and amended.status == "accepted" and amended.client_key == "amend"
    cancelled = broker.cancel_order(cancel(amended))
    assert cancelled.status == "unknown" and cancelled.client_key == "cancel"
    if market == "KR":
        assert server.calls[-1][3]["orgl_oder_no"] == int(placed.id)
    rows = broker.list_orders()
    assert not [r for r in rows if r.status in {"open", "partial"}]
    if market == "KR":
        assert all(r.status == "unknown" and r.remaining is None for r in rows)
        assert server.orders["KR"][amended.id]["ncnt_qty"] == 2  # allocation not invented
    else:
        assert server.calls[-2][1].endswith("/history")
        assert next(r for r in rows if r.id == amended.id).status == "cancelled"
        assert next(r for r in rows if r.id == amended.id).remaining == 0
        assert next(r for r in rows if r.id == placed.id).remaining is None
    assert broker.status(placed).filled == 1  # cancellation must not erase the execution
    balance = broker.get_balance()
    assert balance.position_value == 990
    assert balance.cash_available == (100000 if market == "KR" else 10000) - 990
    assert broker.available_cash() == balance.cash_available
    if market == "US":
        [position] = broker.get_positions()
        assert position.symbol == "BRK.B" and position.quantity_exact == 1
        assert broker.available_qty("BRK.B") == 1
    else:
        with pytest.raises(SpecUnverifiedError, match="sellable quantity"):
            broker.get_positions()  # unknown sellability is neither zero nor the holding quantity


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("venue", ["KRX", "NXT", "SOR"])
@pytest.mark.parametrize("kind,price,stop,native", [
    ("limit", "1000", None, "01"), ("market", None, None, "05"),
    ("conditional_limit", "1000", None, "06"), ("best", None, None, "12"),
    ("priority", None, None, "13"), ("midpoint", None, None, "84"),
    ("stop_limit", "1000", "1100", "83"), ("close", None, None, "71"),
])
def test_kr_kinds_and_venues(world, side, venue, kind, price, stop, native):
    server, broker = world()
    placed = broker.place_order(order(side=side, venue=venue, order_type=kind, price=price, stop_price=stop))
    body = server.calls[-1][3]
    assert (body["iscd"], body["oder_cls_code"], body["exch_kind_code"]) == (
        "A005930", native, {"KRX": "01", "NXT": "02", "SOR": "03"}[venue])
    result = broker.status(placed)
    assert result.remaining == 3
    assert (result.side, result.order_type, result.price) == (side, kind, Decimal(price) if price else None)
    broker.cancel_order(cancel(placed, venue=venue, quantity=3))


@pytest.mark.parametrize("tif,native", [("day", "0"), ("ioc", "3"), ("fok", "4")])
def test_time_in_force(world, tif, native):
    server, broker = world()
    broker.place_order(order(time_in_force=tif))
    assert server.calls[-1][3]["oder_cond_cls_code"] == native


@pytest.mark.parametrize("session,kind,price,native", [
    ("pre", "market", None, "61"), ("after", "limit", "1000", "01"),
    ("after", "best", None, "12"), ("after", "priority", None, "13"),
])
def test_sessions(world, session, kind, price, native):
    server, broker = world()
    broker.place_order(order(session=session, order_type=kind, price=price))
    assert server.calls[-1][3]["oder_cls_code"] == native


@pytest.mark.parametrize("venue,vendor", [("NYSE", "0321"), ("NASDAQ", "0537"), ("AMEX", "0066")])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind,price,native", [("limit", "1000.1234", "01"), ("market", None, "05")])
def test_us_kinds_and_venues(world, venue, vendor, side, kind, price, native):
    server, broker = world("US", venue)
    placed = broker.place_order(order("US", venue=venue, side=side, order_type=kind, price=price))
    body = server.calls[-1][3]
    assert (body["ovrs_stck_symb_iscd"], body["ovrs_exch_vndr_idno"], body["oder_cls_code"],
            body["oder_cond_cls_code"]) == ("BRK.B", vendor, native, "0")
    assert body["ovrs_stck_oder_prc"] == (price if price else "0")
    result = broker.status(placed)
    assert (result.side, result.order_type, result.price) == (side, kind, Decimal(price) if price else None)
    broker.cancel_order(cancel(placed, venue=venue, quantity=3))
    assert server.calls[-1][3]["oder_cls_code"] == native


@pytest.mark.parametrize("code,kind", [("0760", "rejected"), ("3180", "rejected"),
                                       ("EGW00123", "token_expired"), ("EGW00121", "token_expired"),
                                       ("EGW00200", "rate_limited")])
@pytest.mark.parametrize("action", ["place", "amend", "cancel"])
def test_known_errors_and_idempotency(world, code, kind, action):
    server, broker = world()
    placed = broker.place_order(order(client_key="initial"))
    call = {"place": lambda: broker.place_order(order()), "amend": lambda: broker.amend_order(amend(placed)),
            "cancel": lambda: broker.cancel_order(cancel(placed))}[action]
    server.write_failure = code
    with pytest.raises(BrokerRejectedError) as exc:
        call()
    assert exc.value.kind == kind and exc.value.code == code
    n = len(server.calls)
    with pytest.raises(DuplicateOrderError):
        call()
    assert len(server.calls) == n and len(server.orders["KR"]) == 1
    if kind == "token_expired":
        with pytest.raises(ConfigurationError):
            broker.available_cash()
        assert len(server.calls) == n


@pytest.mark.parametrize("market,venue", [("KR", None), ("US", "NYSE")])
@pytest.mark.parametrize("warning", ["1", "2", "3", "h"])
def test_warning_is_not_acceptance_or_automatic_confirmation(world, market, venue, warning):
    server, broker = world(market, venue)
    server.warning = warning
    with pytest.raises(BrokerRejectedError):
        broker.place_order(order(market))
    assert not server.orders[market] and len(server.calls) == 2


@pytest.mark.parametrize("action", ["place", "amend", "cancel"])
@pytest.mark.parametrize("failure", ["EGW00002", "unlisted", "lost"])
def test_uncertain_mutation_never_retransmits(world, action, failure):
    server, broker = world()
    placed = broker.place_order(order(client_key="initial"))
    call = {"place": lambda: broker.place_order(order()), "amend": lambda: broker.amend_order(amend(placed)),
            "cancel": lambda: broker.cancel_order(cancel(placed))}[action]
    if failure == "lost":
        server.lose_next_write = True
    else:
        server.write_failure = failure
    with pytest.raises(TransportError if failure == "lost" else InvalidResponseError):
        call()
    count = len(server.calls)
    with pytest.raises(DuplicateOrderError):
        call()
    assert len(server.calls) == count


def test_expiry_and_quota_are_modeled_without_wall_clock_waiting(world):
    server, broker = world()
    server.now = server.expiry
    with pytest.raises(BrokerRejectedError) as exc:
        broker.place_order(order())
    assert exc.value.kind == "token_expired" and not server.orders["KR"]
    server, broker = world()
    server.limit = 0
    with pytest.raises(BrokerRejectedError) as exc:
        broker.available_cash()
    assert exc.value.kind == "rate_limited"


def test_pagination_preserves_spaces_and_missing_page_evidence_fails(world):
    server, broker = world()
    server.page_size = 1
    broker.place_order(order(client_key="one"))
    broker.place_order(order(client_key="two"))
    assert len(broker.list_orders()) == 2
    assert server.calls[-1][2]["tr_cont_key"] == "1  "
    path = "/trading/v1/orders/history"
    server.overrides[path] = {"rsp_cd": "5762", "data": [], "tr_cont": "1"}
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


def test_no_data_code_does_not_discard_data(world):
    server, broker = world()
    path = "/accounts/v1/me/deposit"
    server.overrides[path] = {"rsp_cd": "5820", "data": {"oder_able_cash_amt": 123}}
    assert broker.available_cash() == 123


def test_us_unfilled_uses_documented_difference_not_ncnt_odqt(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    server.orders["US"][placed.id]["ncnt_odqt"] = 0
    assert broker.status(placed).remaining == 3


def test_unsupported_or_unverified_inputs_send_nothing(world):
    server, broker = world()
    before = len(server.calls)
    for kw in [{"session": "single_price"}, {"session": "pre"},
               {"session": "after", "order_type": "market", "price": None}]:
        with pytest.raises(NotSupportedError):
            broker.place_order(order(**kw))
    assert len(server.calls) == before
    server, broker = world("US", "NYSE")
    with pytest.raises(ConfigurationError):
        broker.place_order(order("US", price="1.00001"))
    with pytest.raises(NotSupportedError):
        broker.place_order(order("US", quantity=None, amount="10.00", order_type="market", price=None))
    assert len(server.calls) == 1


def test_mock_or_wrong_host_is_refused(world):
    server, broker = world()
    broker.virtual = True
    with pytest.raises(ConfigurationError):
        broker.available_cash()
    broker.virtual = False
    server.base_url = "https://example.invalid"
    with pytest.raises(ConfigurationError):
        broker.available_cash()


def test_http500_json_code_is_preserved_and_malformed_transport_is_sanitized(monkeypatch):
    import pykbroker.brokers.meritz as module
    def refused(*args, **kw):
        raise HTTPError("https://fixture.invalid", 500, "fixture", {}, BytesIO(b'{"rsp_cd":"0760"}'))
    monkeypatch.setattr(module, "urlopen", refused)
    assert _MeritzRestClient().request("GET", "/fixture", headers={}) == {"rsp_cd": "0760"}
    def broken(*args, **kw):
        raise RuntimeError("fixture-sensitive-value")
    monkeypatch.setattr(module, "urlopen", broken)
    with pytest.raises(TransportError) as exc:
        _MeritzRestClient().request("GET", "/fixture", headers={})
    assert exc.value.__context__ is None and "fixture-sensitive" not in str(exc.value)


def test_schema_validator_rejects_wrong_field_type_and_unknown_account():
    schema = SPEC["paths"]["/trading/v1/orders/buy"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    body = {"iscd": "A005930", "oder_unpr": 1000, "odqt": 1, "oder_cls_code": "01",
            "oder_cond_cls_code": "0", "exch_kind_code": "01"}
    validate(body, schema)
    with pytest.raises(AssertionError):
        validate(body | {"odqt": "1"}, schema)
    with pytest.raises(AssertionError):
        validate(body | {"account": "fixture-account"}, schema)


@pytest.mark.parametrize("market,venue", [("KR", None), ("US", "NYSE")])
def test_empty_account_and_order_book_are_real_empty_lists(world, market, venue):
    _, broker = world(market, venue)
    assert broker.list_orders() == [] and broker.get_positions() == []
    assert broker.get_balance().position_value == 0


@pytest.mark.parametrize("market,venue", [("KR", None), ("US", "NYSE")])
def test_full_fill_is_reconciled_only_from_ledger(world, market, venue):
    server, broker = world(market, venue)
    placed = broker.place_order(order(market))
    server.fill(market, placed.id, 3, 990)
    result = broker.status(placed)
    assert (result.status, result.filled, result.remaining) == ("filled", 3, 0)
    assert result.fill_price is None  # source calls it execution price, not weighted average


def test_identity_mismatch_blocks_amend_and_cancel_before_write(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    before = sum(call[0] == "POST" for call in server.calls)
    with pytest.raises(InvalidResponseError):
        broker.amend_order(amend(placed, symbol="AAPL"))
    with pytest.raises(InvalidResponseError):
        broker.cancel_order(cancel(placed, symbol="AAPL"))
    assert sum(call[0] == "POST" for call in server.calls) == before


@pytest.mark.parametrize("data", [
    {"rsp_cd": "0001", "data": {"oder_no": 1}},
    {"rsp_cd": "0001", "data": {"oder_no": 0, "warn_cls_code": "0"}},
    {"rsp_cd": "5766", "data": {"oder_no": 1, "warn_cls_code": "0"}},
    {"rsp_cd": "0001", "data": None},
])
def test_malformed_success_remains_unknown_and_key_stays_claimed(world, data):
    server, broker = world()
    server.overrides["/trading/v1/orders/buy"] = data
    with pytest.raises(InvalidResponseError):
        broker.place_order(order())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(order())
    assert len(server.calls) == 2


def test_actual_serializer_get_query_and_post_numeric_body(monkeypatch):
    import json

    import pykbroker.brokers.meritz as module

    requests = []
    def receive(req, timeout):
        requests.append(req)
        return BytesIO(b'{"rsp_cd":"5766","data":[]}')
    monkeypatch.setattr(module, "urlopen", receive)
    client = _MeritzRestClient()
    client.request("GET", "/fixture", headers={}, params={"tr_cont": "1", "tr_cont_key": "key  "})
    assert requests[-1].full_url.endswith("tr_cont=1&tr_cont_key=key++")
    client.request("POST", "/fixture", headers={}, body={"odqt": 3, "oder_unpr": 1000})
    assert json.loads(requests[-1].data) == {"odqt": 3, "oder_unpr": 1000}


def test_rate_is_ten_per_endpoint_and_credentials_share_budget(world):
    _, one = world()
    _, two = world()
    one.available_cash()
    assert one._limiter is two._limiter
    assert one._limiter._rates["/accounts/v1/me/deposit"] == 10.0


def test_us_holdings_retains_fractional_precision(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    server.fill("US", placed.id)
    server.holdings["US"][0].update(rmnd_dqty="1.1234567890", seln_able_dqty="0.9876543210")
    [p] = broker.get_positions()
    assert (p.quantity, p.sellable, p.quantity_exact, p.sellable_exact) == (
        1, 0, Decimal("1.1234567890"), Decimal("0.9876543210"))


def test_us_prices_are_fixed_point_with_no_false_extra_precision(world):
    server, broker = world("US", "NYSE")
    broker.place_order(order("US", price="1.23000"))
    assert server.calls[-1][3]["ovrs_stck_oder_prc"] == "1.23"
    broker.place_order(order("US", price=None, order_type="market", client_key="market"))
    assert server.calls[-1][3]["ovrs_stck_oder_prc"] == "0"


def test_unknown_original_exchange_never_defaults_to_krx(world):
    server, broker = world()
    placed = broker.place_order(order(venue="NXT"))
    before = len(server.calls)
    with pytest.raises(ConfigurationError, match="original KR venue"):
        broker.cancel_order(cancel(placed, venue=None))
    assert len(server.calls) == before


def test_us_mutation_checks_the_original_exchange(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    server.orders["US"][placed.id]["iscd"] = "BRK.B.OQ"
    with pytest.raises(InvalidResponseError, match="exchange"):
        broker.amend_order(amend(placed))
    assert len(server.orders["US"]) == 1


def test_us_cancel_old_number_resolves_latest_amendment(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    amended = broker.amend_order(amend(placed))
    broker.cancel_order(cancel(placed))
    assert server.calls[-1][3]["orgl_oder_no"] == int(amended.id)
    assert broker.status(amended).status == "cancelled"


def test_us_replaced_without_today_evidence_is_unknown(world):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    server.fill("US", placed.id)
    amended = broker.amend_order(amend(placed))
    broker.cancel_order(cancel(amended))
    server.today.clear()
    rows = broker.list_orders()
    assert all(r.status == "unknown" and r.remaining is None for r in rows)


@pytest.mark.parametrize("field,value", [("cncl_cnfr_qty", 0), ("ncnt_qty", 1), ("oder_able_qty", 1)])
def test_us_cancellation_requires_all_three_today_fields(world, field, value):
    server, broker = world("US", "NYSE")
    placed = broker.place_order(order("US"))
    broker.cancel_order(cancel(placed, quantity=3))
    server.today[placed.id][field] = value
    result = broker.status(placed)
    assert (result.status, result.remaining) == ("unknown", None)


def test_kr_quote_degraded_code_requires_data_and_matching_symbol(world):
    server, broker = world()
    path = "/market/v1/prices"
    server.overrides[path] = {"rsp_cd": "\x00\x00", "data": {"shrn_iscd": "005930", "stck_prpr": "70000"}}
    assert broker.get_quote("005930").last == 70000
    server.overrides[path]["data"]["shrn_iscd"] = "000660"
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")
    server.overrides[path] = {"rsp_cd": "\x00"}
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")
    server.overrides["/trading/v1/orders/buy"] = {"rsp_cd": "\x00", "data": {"oder_no": 1, "warn_cls_code": "0"}}
    with pytest.raises(InvalidResponseError):
        broker.place_order(order())


def test_kr_rejected_keeps_status_with_reason(world):
    server, broker = world()
    placed = broker.place_order(order())
    server.orders["KR"][placed.id].update(oder_pros_stat_cls_code="3", oder_rfus_reas_code="S607")
    assert broker.status(placed).status == "rejected"
