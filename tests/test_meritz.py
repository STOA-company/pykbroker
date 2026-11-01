"""Meritz adapter, offline: HTTP is replaced by a scripted fake client at the adapter's one boundary.

`tests/fixtures/meritz/*.json` are SYNTHETIC responses built from the official openapi.json (2026-10-28) schema; no
real Meritz response was captured. They pin field names and shapes only. What the spec does not say is refused or
reported unknown, never guessed (see the module docstring of pykbroker.brokers.meritz).
"""

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pykbroker.brokers import meritz as module
from pykbroker.brokers.meritz import Meritz
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import CancelRequest, Order, OrderRequest

FIX = Path(__file__).parent / "fixtures" / "meritz"
NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # 월 10:00 KST
OPTIONS = {"app_key": "fixture-app-key", "app_secret": "fixture-app-secret"}
HISTORY = "/trading/v1/orders/history"
US_HISTORY = "/trading/v1/overseas/orders/history"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeClient:
    """`responses[(method, path)]` is a list consumed in order; an Exception in it is raised (a lost answer);
    the last entry repeats so a read can be asked many times."""

    def __init__(self, base_url, responses):
        self.base_url, self.calls, self.responses = base_url, [], {k: list(v) for k, v in responses.items()}

    def request(self, method, path, *, headers, params=None, body=None, form=False):
        self.calls.append(
            {"method": method, "path": path, "headers": dict(headers), "params": params, "body": body, "form": form}
        )
        queue = self.responses[(method, path)]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, tuple) else (200, item)

    def count(self, method, path):
        return sum(1 for c in self.calls if (c["method"], c["path"]) == (method, path))


class NoWait:
    def __init__(self):
        self.waits = []

    def wait(self, tr):
        self.waits.append(tr)


@pytest.fixture(autouse=True)
def fresh_tokens(monkeypatch):
    module._TOKENS.clear()
    yield
    module._TOKENS.clear()


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(module, "MERITZ_SPEC_VERIFIED", True)


def make(responses=None, *, options=None, safety=None, allow=True, market="KR", venue=None, **kwargs):
    made, slept = [], []
    scripted = {("POST", "/oauth2/token"): [fx("token")], **(responses or {})}

    def factory(*, base_url):
        made.append(FakeClient(base_url, scripted))
        return made[0]

    broker = Meritz(
        account="fixture-account",
        client_options=options or OPTIONS,
        client_factory=factory,
        rate_limiter=NoWait(),
        safety=safety,
        allow_live_orders=allow,
        market=market,
        venue=venue,
        clock=lambda: NOW,
        sleeper=slept.append,
        **kwargs,
    )
    return broker, made[0], slept


def buy(key="k1", **over):
    base = {"symbol": "005930", "side": "buy", "quantity": 1, "price": "70000", "client_key": key}
    return OrderRequest(**{**base, **over})


def ok(body, status=200):
    return (status, body)


ORDER_BUY = ("POST", "/trading/v1/orders/buy")


# ----------------------------------------------------------------------------- locks and construction
def test_writes_are_locked_by_default_and_send_nothing(live_safety):
    broker, client, _ = make({ORDER_BUY: [fx("order_accept")]}, safety=live_safety)
    with pytest.raises((SpecUnverifiedError, NotSupportedError)):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 0 and module.MERITZ_SPEC_VERIFIED is False


def test_module_switch_alone_does_not_open_writes(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [fx("order_accept")]}, safety=live_safety, allow=False)
    with pytest.raises((SpecUnverifiedError, NotSupportedError)):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 0


def test_instance_flag_alone_does_not_open_writes(live_safety):
    broker, client, _ = make({ORDER_BUY: [fx("order_accept")]}, safety=live_safety, allow=True)
    assert module.MERITZ_SPEC_VERIFIED is False
    with pytest.raises((SpecUnverifiedError, NotSupportedError)):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 0


def test_cancel_is_locked_too(live_safety):
    broker, client, _ = make({("POST", "/trading/v1/orders/cancel"): [fx("order_accept")]}, safety=live_safety)
    with pytest.raises(SpecUnverifiedError):
        broker.cancel_order(CancelRequest(order_id="1", symbol="005930", quantity=1, client_key="c1"))
    assert client.count("POST", "/trading/v1/orders/cancel") == 0


def test_virtual_account_is_refused():
    with pytest.raises(ConfigurationError):
        make(virtual=True)


@pytest.mark.parametrize(
    "options",
    [
        {"app_key": "k" * 37, "app_secret": "s"},
        {"app_key": "k"},  # neither token nor secret
        {"app_key": "k", "app_secret": "s", "surprise": 1},
        {"app_key": "k", "app_secret": "s", "token_type": "Basic"},
        {"app_key": "k", "app_secret": "s", "mac_address": "not-a-mac"},
    ],
)
def test_bad_client_options_are_refused(options):
    with pytest.raises(ConfigurationError):
        make(options=options)


def test_us_needs_a_known_venue():
    with pytest.raises(ConfigurationError):
        make(market="US", venue=None)
    with pytest.raises(ConfigurationError):
        make(market="US", venue="TSE")


def test_registered_in_the_shared_factory():
    assert BROKERS["meritz"] is Meritz
    broker = create_broker(
        "meritz",
        account="a",
        client_options=OPTIONS,
        client_factory=lambda **kw: FakeClient(kw["base_url"], {}),
        rate_limiter=NoWait(),
    )
    assert broker.broker_code == "meritz"


def test_capabilities_probe_without_an_instance(unlocked):
    probe = Meritz.__new__(Meritz)
    probe.market, probe.virtual, probe.venue = "KR", False, "KRX"
    caps = probe.capabilities()
    assert caps.supports_single_order_cancel is True and caps.supports_virtual_account is False
    assert caps.supports_market_order is False and caps.supports_nxt is False and caps.supports_limit_order is False
    probe.market, probe.venue = "US", "KRX"
    assert probe.capabilities().requires_venue is True and "NASDAQ" in probe.capabilities().venues


def test_live_capabilities(unlocked):
    broker, _, _ = make()
    caps = broker.capabilities()
    assert caps.supports_limit_order and not caps.supports_market_order and caps.markets == ("KR",)


# ----------------------------------------------------------------------------- KR orders
def test_kr_limit_buy_body_headers_and_acceptance(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [fx("order_accept")]}, safety=live_safety)
    order = broker.place_order(buy())
    assert (order.id, order.status, order.symbol, order.price) == ("1234", "accepted", "005930", Decimal(70000))
    post = [c for c in client.calls if c["path"] == ORDER_BUY[1]][0]
    assert post["body"] == {
        "iscd": "A005930",
        "oder_unpr": 70000,
        "odqt": 1,
        "oder_cls_code": "01",
        "oder_cond_cls_code": "0",
        "orgl_oder_no": 0,
        "whol_rctf_cncl_yn": "N",
        "warn_cnfr_yn": "N",
        "exch_kind_code": "01",
    }
    assert post["headers"]["authorization"] == "Bearer fixture-access-token"
    assert len(post["headers"]["mac_address"]) == 12 and "acno" not in post["body"]
    assert client.count(*ORDER_BUY) == 1


def test_kr_sell_goes_to_the_sell_path(unlocked, live_safety):
    broker, client, _ = make({("POST", "/trading/v1/orders/sell"): [fx("order_accept")]}, safety=live_safety)
    assert broker.place_order(buy(side="sell")).status == "accepted"
    assert client.count("POST", "/trading/v1/orders/sell") == 1


def test_market_order_is_not_supported(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [fx("order_accept")]}, safety=live_safety)
    request = OrderRequest(symbol="005930", side="buy", quantity=1, order_type="market", client_key="m1")
    with pytest.raises(NotSupportedError):
        broker.place_order(request)
    assert client.count(*ORDER_BUY) == 0


@pytest.mark.parametrize("warn", ["1", "2", "3", "4", "6", "7", "9", "a", "c", "d", "f", "g", "b", "h"])
def test_any_warning_is_not_accepted_and_is_never_resent(unlocked, live_safety, warn):
    body = copy.deepcopy(fx("order_warn"))
    body["data"]["warn_cls_code"] = warn
    broker, client, _ = make({ORDER_BUY: [body]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(buy())
    assert caught.value.reason == "meritz_warning" and caught.value.warn_message == "fixture warning text"
    assert client.count(*ORDER_BUY) == 1  # warn_cnfr_yn=Y is never sent on its own
    assert all(c["body"]["warn_cnfr_yn"] == "N" for c in client.calls if c["path"] == ORDER_BUY[1])


def test_acceptance_without_warn_field_is_not_proof(unlocked, live_safety):
    body = {"rsp_cd": "0001", "rsp_msg": "x", "data": {"oder_no": 5}}
    broker, client, _ = make({ORDER_BUY: [body]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 1


@pytest.mark.parametrize("code", ["0760", "EGW00103", "3180", "IGW50024"])
def test_explicit_refusal_on_http_500_is_definite(unlocked, live_safety, code):
    broker, client, _ = make({ORDER_BUY: [(500, {"rsp_cd": code, "rsp_msg": "x"})]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(buy())
    assert caught.value.code == code and caught.value.kind == "rejected"
    assert client.count(*ORDER_BUY) == 1


def test_server_error_and_unknown_code_on_a_write_are_unknown(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [(500, {"rsp_cd": "EGW00002", "rsp_msg": "x"})]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 1


def test_lost_answer_is_unknown_and_the_same_key_cannot_send_twice(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [TimeoutError("lost"), fx("order_accept")]}, safety=live_safety)
    with pytest.raises(TransportError):
        broker.place_order(buy("same"))
    with pytest.raises(DuplicateOrderError):
        broker.place_order(buy("same"))
    assert client.count(*ORDER_BUY) == 1


def test_unreadable_answer_is_unknown(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [(200, "<html>")]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        broker.place_order(buy())
    assert client.count(*ORDER_BUY) == 1


def test_write_rate_limit_is_a_rejection_and_not_resent(unlocked, live_safety):
    broker, client, slept = make({ORDER_BUY: [(500, fx("error_rate_limit"))]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(buy())
    assert caught.value.kind == "rate_limited" and client.count(*ORDER_BUY) == 1 and slept == []


def test_expired_token_at_a_write_is_one_post_and_the_next_order_reissues(unlocked, live_safety):
    broker, client, _ = make({ORDER_BUY: [(500, fx("error_token_expired")), fx("order_accept")]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(buy("a"))
    assert caught.value.kind == "token_expired" and client.count(*ORDER_BUY) == 1
    assert broker.place_order(buy("b")).status == "accepted"
    assert client.count("POST", "/oauth2/token") == 2 and client.count(*ORDER_BUY) == 2


def test_the_sanitized_error_carries_no_vendor_text(unlocked, live_safety):
    body = {"rsp_cd": "0760", "rsp_msg": "SECRET-SENTENCE", "rsp_sub_msg": "ACCOUNT-123"}
    broker, _, _ = make({ORDER_BUY: [(500, body)]}, safety=live_safety)
    with pytest.raises(BrokerRejectedError) as caught:
        broker.place_order(buy())
    assert "SECRET" not in str(caught.value) and "ACCOUNT" not in str(caught.value)


def test_kr_cancel_body_and_ack(unlocked, live_safety):
    broker, client, _ = make({("POST", "/trading/v1/orders/cancel"): [fx("order_accept")]}, safety=live_safety)
    order = broker.cancel_order(CancelRequest(order_id="1234", symbol="005930", quantity=2, client_key="c"))
    assert order.id == "1234" and order.status == "accepted"
    body = client.calls[-1]["body"]
    assert (body["orgl_oder_no"], body["odqt"], body["iscd"], body["exch_kind_code"]) == (1234, 2, "A005930", "01")
    assert client.count("POST", "/trading/v1/orders/cancel") == 1


def test_kr_modify_uses_the_new_price_and_returns_the_new_number(unlocked, live_safety):
    answer = copy.deepcopy(fx("order_accept"))
    answer["data"]["oder_no"] = 1300
    broker, client, _ = make({("POST", "/trading/v1/orders/modify"): [answer]}, safety=live_safety)
    order = broker.modify_order("1234", buy("mod", price="69900", quantity=1))
    assert order.id == "1300" and order.price == Decimal(69900)
    body = client.calls[-1]["body"]
    assert (body["orgl_oder_no"], body["oder_unpr"], body["odqt"], body["whol_rctf_cncl_yn"]) == (1234, 69900, 1, "N")
    with pytest.raises(DuplicateOrderError):
        broker.modify_order("1234", buy("mod", price="69900", quantity=1))
    assert client.count("POST", "/trading/v1/orders/modify") == 1


def test_modify_is_locked(live_safety):
    broker, client, _ = make({("POST", "/trading/v1/orders/modify"): [fx("order_accept")]}, safety=live_safety)
    with pytest.raises((SpecUnverifiedError, NotSupportedError)):
        broker.modify_order("1234", buy("mod"))
    assert client.count("POST", "/trading/v1/orders/modify") == 0


# ----------------------------------------------------------------------------- token
def test_token_is_issued_once_and_reused_across_instances():
    quote = {("GET", "/market/v1/prices"): [fx("prices_kr")], ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    first, c1, _ = make(quote)
    first.get_quote("005930")
    first.get_quote("005930")
    second, c2, _ = make(quote)
    second.get_quote("005930")
    assert c1.count("POST", "/oauth2/token") == 1 and c2.count("POST", "/oauth2/token") == 0


def test_stored_token_is_used_until_its_margin_and_then_reissued():
    quote = {("GET", "/market/v1/prices"): [fx("prices_kr")], ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    options = {**OPTIONS, "token": "stored-token", "expires_at": NOW + timedelta(hours=1)}
    broker, client, _ = make(quote, options={**options, "expires_at": NOW.replace(tzinfo=None) + timedelta(hours=10)})
    broker.get_quote("005930")
    assert client.calls[0]["headers"]["authorization"] == "Bearer stored-token"
    old, client, _ = make(quote, options={**options, "expires_at": NOW + timedelta(seconds=30)})
    old.get_quote("005930")
    assert client.count("POST", "/oauth2/token") == 1


def test_token_request_is_form_encoded_as_the_spec_says():
    broker, client, _ = make(
        {("GET", "/market/v1/prices"): [fx("prices_kr")], ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    )
    broker.get_quote("005930")
    assert (
        client.calls[0]["form"] is True
        and client.calls[0]["headers"]["content-type"] == "application/x-www-form-urlencoded"
    )


def test_quote_with_a_damaged_rsp_cd_is_judged_by_its_data():
    body = copy.deepcopy(fx("prices_kr"))
    body["rsp_cd"] = "00\x000\x00"
    broker, _, _ = make({("GET", "/market/v1/prices"): [body], ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]})
    assert broker.get_quote("005930").last == Decimal(70000)
    broker, _, _ = make({("GET", "/market/v1/prices"): [{"rsp_cd": "9999", "rsp_msg": "x"}]})
    with pytest.raises(BrokerRejectedError):  # no data: not a success
        broker.get_quote("005930")


def test_token_issue_sends_the_documented_body_and_bad_key_is_a_refusal():
    broker, client, _ = make(
        {("POST", "/oauth2/token"): [(403, fx("error_bad_key"))], ("GET", "/market/v1/prices"): [fx("prices_kr")]}
    )
    with pytest.raises(BrokerRejectedError) as caught:
        broker.get_quote("005930")
    assert caught.value.code == "EGW00103"
    assert client.calls[0]["body"] == {
        "grant_type": "client_credentials",
        "client_id": "fixture-app-key",
        "client_secret": "fixture-app-secret",
        "scope": "login",
    }
    assert "fixture-app-secret" not in str(caught.value)


def test_expired_token_on_a_read_reissues_once_and_retries():
    prices = [(500, fx("error_token_expired")), fx("prices_kr")]
    broker, client, _ = make(
        {("GET", "/market/v1/prices"): prices, ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    )
    assert broker.get_quote("005930").last == Decimal(70000)
    assert client.count("POST", "/oauth2/token") == 2


def test_a_token_refused_twice_stops():
    prices = [(500, fx("error_token_expired"))]
    broker, client, _ = make({("GET", "/market/v1/prices"): prices})
    with pytest.raises(BrokerRejectedError):
        broker.get_quote("005930")
    assert client.count("GET", "/market/v1/prices") == 2


def test_read_rate_limit_waits_and_retries_then_gives_up():
    prices = [(500, fx("error_rate_limit")), fx("prices_kr")]
    broker, client, slept = make(
        {("GET", "/market/v1/prices"): prices, ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    )
    assert broker.get_quote("005930").last == Decimal(70000) and len(slept) == 1
    broker, client, slept = make({("GET", "/market/v1/prices"): [(500, fx("error_rate_limit"))]})
    with pytest.raises(BrokerRejectedError) as caught:
        broker.get_quote("005930")
    assert caught.value.kind == "rate_limited" and client.count("GET", "/market/v1/prices") == 3


def test_every_request_waits_on_the_shared_limiter_by_path():
    broker, client, _ = make(
        {("GET", "/market/v1/prices"): [fx("prices_kr")], ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")]}
    )
    broker.get_quote("005930")
    assert broker._limiter.waits == ["/oauth2/token", "/market/v1/prices", "/market/v1/orderbook"]
    assert module.MERITZ_RATES["/market/v1/prices"] == 10.0 and set(module.WRITE_PATHS) <= set(module.MERITZ_RATES)


def test_the_limiter_is_shared_by_app_key_digest():
    from pykbroker.safety.rate_limit import credential_rate_limiter

    one = credential_rate_limiter("meritz", "fixture-shared-key", module.MERITZ_RATES)
    two = credential_rate_limiter("meritz", "fixture-shared-key", module.MERITZ_RATES)
    assert one is two


# ----------------------------------------------------------------------------- reads
def test_kr_quote_rules_cash():
    broker, _, _ = make(
        {
            ("GET", "/market/v1/prices"): [fx("prices_kr")],
            ("GET", "/market/v1/orderbook"): [fx("orderbook_kr")],
            ("GET", "/accounts/v1/me/deposit"): [fx("deposit")],
        },
        halt_provider=lambda symbol: False,
    )
    quote = broker.get_quote("A005930")
    assert (quote.symbol, quote.last, quote.bid, quote.ask) == ("005930", 70000, 69900, 70100)
    rules = broker.get_rules("005930", now=NOW)
    assert rules.halted is False and rules.limit_up == 91000 and rules.limit_down == 49000
    assert broker.available_cash() == Decimal(900000)


def test_native_halt_beats_a_caller_all_clear():
    body = copy.deepcopy(fx("prices_kr"))
    body["data"]["tmpr_susp_yn"] = "Y"
    broker, _, _ = make({("GET", "/market/v1/prices"): [body]}, halt_provider=lambda symbol: False)
    assert broker.get_rules("005930", now=NOW).halted is True


def test_cash_is_capped_by_the_cash_balance():
    body = copy.deepcopy(fx("deposit"))
    body["data"]["oder_able_cash_amt"] = 5_000_000
    broker, _, _ = make({("GET", "/accounts/v1/me/deposit"): [body]})
    assert broker.available_cash() == Decimal(1000000)


def test_positions_skip_credit_rows_and_hold_back_open_sells():
    history = copy.deepcopy(fx("history_kr"))
    history["data"][0].update(deal_cls_code="1", odqt=2, ncnt_qty=2, total_cntg_qty=0)
    broker, _, _ = make({("GET", "/accounts/v1/me/holdings"): [fx("holdings_kr")], ("GET", HISTORY): [history]})
    positions = broker.get_positions()
    assert [(p.symbol, p.quantity, p.sellable) for p in positions] == [("005930", 5, 3)]
    assert broker.available_qty("005930") == 3


def test_positions_when_data_comes_with_5820():
    body = copy.deepcopy(fx("holdings_kr"))
    body["rsp_cd"] = "5820"
    broker, _, _ = make(
        {("GET", "/accounts/v1/me/holdings"): [body], ("GET", HISTORY): [{"rsp_cd": "5820", "rsp_msg": "x"}]}
    )
    assert len(broker.get_positions()) == 1  # data decides, not the code


def test_empty_5820_is_an_empty_list():
    broker, _, _ = make(
        {
            ("GET", "/accounts/v1/me/holdings"): [{"rsp_cd": "5820", "rsp_msg": "x"}],
            ("GET", HISTORY): [{"rsp_cd": "5820"}],
        }
    )
    assert broker.get_positions() == []


def test_balance_sums_positions():
    broker, _, _ = make(
        {
            ("GET", "/accounts/v1/me/holdings"): [fx("holdings_kr")],
            ("GET", HISTORY): [{"rsp_cd": "5820"}],
            ("GET", "/accounts/v1/me/deposit"): [fx("deposit")],
        }
    )
    balance = broker.get_balance()
    assert (balance.currency, balance.cash_available, balance.position_value) == (
        "KRW",
        Decimal(900000),
        Decimal(350000),
    )


def test_paging_sends_the_key_back_byte_for_byte_and_stops():
    first = {"rsp_cd": "5762", "tr_cont": "1", "tr_cont_key": "KEY-1   ", "data": fx("holdings_kr")["data"][:1]}
    second = {"rsp_cd": "5766", "tr_cont": "0", "data": []}
    broker, client, _ = make(
        {("GET", "/accounts/v1/me/holdings"): [first, second], ("GET", HISTORY): [{"rsp_cd": "5820"}]}
    )
    assert len(broker.get_positions()) == 1
    asked = [c["params"] for c in client.calls if c["path"] == "/accounts/v1/me/holdings"]
    assert asked[1]["tr_cont"] == "1" and asked[1]["tr_cont_key"] == "KEY-1   "


def test_a_repeating_continuation_key_is_refused():
    page = {"rsp_cd": "5762", "tr_cont": "1", "tr_cont_key": "SAME", "data": []}
    broker, _, _ = make({("GET", "/accounts/v1/me/holdings"): [page]})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_one_position_listed_twice_with_different_values_is_refused():
    rows = fx("holdings_kr")["data"][:1]
    other = copy.deepcopy(rows[0])
    other["rmnd_qty"] = 6
    page1 = {"rsp_cd": "5762", "tr_cont": "1", "tr_cont_key": "K", "data": rows}
    page2 = {"rsp_cd": "5766", "tr_cont": "0", "data": [other]}
    broker, _, _ = make({("GET", "/accounts/v1/me/holdings"): [page1, page2]})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


# ----------------------------------------------------------------------------- order list and status
def row(number, qty, filled, remaining, kind="1", origin=0, cancelled=0, rewritten=0, stat="1", side="2"):
    return {
        "oder_no": number,
        "iscd": "A005930",
        "deal_cls_code": side,
        "odqt": qty,
        "oder_unpr": 70000,
        "total_cntg_qty": filled,
        "cntg_unpr": 70000 if filled else 0,
        "ncnt_qty": remaining,
        "oder_pros_stat_cls_code": stat,
        "orgl_oder_no": origin,
        "rctf_cncl_cls_code": kind,
        "rctf_qty": rewritten,
        "cncl_qty": cancelled,
        "oder_cls_code": "01",
    }


def history(*rows):
    return {"rsp_cd": "5766", "tr_cont": "0", "data": list(rows)}


def status_of(rows):
    broker, _, _ = make({("GET", HISTORY): [history(*rows)]})
    return {o.id: (o.status, o.filled, o.remaining) for o in broker.list_orders("005930")}


def test_order_statuses_from_the_list():
    got = status_of(
        [
            row(1, 3, 0, 3),
            row(2, 3, 1, 2),
            row(3, 3, 3, 0),
            row(4, 3, 0, 0, stat="3"),
            row(5, 3, 1, 0, cancelled=2),
            row(50, 2, 0, 0, kind="3", origin=5, cancelled=2),
            row(6, 3, 0, 0, cancelled=3),
            row(60, 3, 0, 0, kind="3", origin=6, cancelled=3),
            row(7, 3, 0, 3, rewritten=3),
            row(70, 3, 0, 3, kind="2", origin=7, rewritten=3),
        ]
    )
    assert got[("1")] == ("open", 0, 3) and got["2"] == ("partial", 1, 2) and got["3"] == ("filled", 3, 0)
    assert got["4"][0] == "rejected" and got["5"] == ("cancelled", 1, 0) and got["6"] == ("cancelled", 0, 0)
    assert got["7"] == ("unknown", 0, None) and "50" not in got and "70" not in got  # KR modify chains: never guessed


def test_numbers_that_do_not_add_up_are_unknown():
    assert status_of([row(1, 3, 1, 5)])["1"][0] == "unknown"  # the venue's remainder disagrees
    assert (
        status_of([row(3, 3, 3, 0, cancelled=1), row(30, 1, 0, 0, kind="3", origin=3, cancelled=1)])["3"][0]
        == "unknown"
    )


def test_status_of_an_unlisted_order_is_unknown_never_filled():
    broker, _, _ = make({("GET", HISTORY): [history(row(1, 3, 0, 3))]})
    ghost = Order(id="99", symbol="005930", status="accepted")
    assert broker.status(ghost).status == "unknown"
    assert broker.status(Order(id="1", symbol="005930", status="accepted")).status == "open"


def test_a_duplicated_order_number_is_unknown():
    broker, _, _ = make({("GET", HISTORY): [history(row(1, 3, 0, 3), row(1, 3, 3, 0))]})
    assert broker.status(Order(id="1", symbol="005930", status="accepted")).status == "unknown"


# ----------------------------------------------------------------------------- US
US = {"market": "US", "venue": "NASDAQ"}


@pytest.fixture
def us_safety():
    from pykbroker.safety.gate import OrderSafety
    from pykbroker.safety.idempotency import MemoryIdempotencyStore
    from pykbroker.safety.kill_switch import KillSwitch
    from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="1000000", daily_krw_cap="2000000"),
        risk_ledger=MemoryRiskLedger(),
        idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda broker, account: False),
        valuation=lambda request: Decimal(600000),
    )


def us_buy(key="u1", **over):
    base = {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 2,
        "price": "190.0500",
        "market": "US",
        "venue": "NASDAQ",
        "client_key": key,
    }
    return OrderRequest(**{**base, **over})


def test_us_quote_cash_positions():
    broker, _, _ = make(
        {
            ("GET", "/market/v1/overseas/prices"): [fx("prices_us")],
            ("GET", "/market/v1/overseas/orderbook"): [fx("orderbook_us")],
            ("GET", "/trading/v1/overseas/orders/estimate"): [fx("estimate_us")],
            ("GET", "/accounts/v1/me/overseas/holdings"): [fx("holdings_us")],
        },
        **US,
    )
    quote = broker.get_quote("AAPL", market="US")
    assert (quote.last, quote.bid, quote.ask, quote.market) == (190, Decimal("189.95"), Decimal("190.05"), "US")
    assert broker.available_cash() == Decimal("4800.00")
    positions = broker.get_positions()
    assert [(p.symbol, p.quantity, p.sellable, p.market) for p in positions] == [("AAPL", 4, 4, "US")]


def test_us_quote_request_names_exchange_and_realtime():
    broker, client, _ = make(
        {
            ("GET", "/market/v1/overseas/prices"): [fx("prices_us")],
            ("GET", "/market/v1/overseas/orderbook"): [fx("orderbook_us")],
        },
        **US,
    )
    broker.get_quote("AAPL", market="US")
    params = [c["params"] for c in client.calls if c["path"] == "/market/v1/overseas/prices"][0]
    assert params == {"mrkt_div_code": "OV", "mrkt_cls_code": "OQ", "iscd": "AAPL", "dely_rltm_cls_code": "0"}


def test_us_limit_buy_body(unlocked, us_safety):
    path = ("POST", "/trading/v1/overseas/orders/buy")
    broker, client, _ = make({path: [fx("order_accept")]}, safety=us_safety, **US)
    order = broker.place_order(us_buy())
    assert (order.id, order.market, order.status) == ("1234", "US", "accepted")
    assert client.calls[-1]["body"] == {
        "ovrs_stck_symb_iscd": "AAPL",
        "ovrs_exch_vndr_idno": "0537",
        "oder_cls_code": "01",
        "oder_cond_cls_code": "0",
        "odqt": 2,
        "ovrs_stck_oder_prc": "190.0500",
        "warn_cnfr_yn": "N",
    }


def test_us_price_with_more_than_four_decimals_is_refused_before_http(unlocked, us_safety):
    path = ("POST", "/trading/v1/overseas/orders/buy")
    broker, client, _ = make({path: [fx("order_accept")]}, safety=us_safety, **US)
    with pytest.raises(ConfigurationError):
        broker.place_order(us_buy(price="190.00001"))
    assert client.count(*path) == 0


def test_us_warning_two_is_hold_not_accepted(unlocked, us_safety):
    path = ("POST", "/trading/v1/overseas/orders/buy")
    body = copy.deepcopy(fx("order_warn"))
    body["data"]["warn_cls_code"] = "2"
    broker, client, _ = make({path: [body]}, safety=us_safety, **US)
    with pytest.raises(BrokerRejectedError):
        broker.place_order(us_buy())
    assert client.count(*path) == 1


def test_us_cancel_looks_up_the_original_order_date(unlocked, us_safety):
    cancel = ("POST", "/trading/v1/overseas/orders/cancel")
    broker, client, _ = make(
        {cancel: [fx("order_accept")], ("GET", US_HISTORY): [fx("history_us")]}, safety=us_safety, **US
    )
    order = broker.cancel_order(
        CancelRequest(order_id="77", symbol="AAPL", quantity=2, market="US", venue="NASDAQ", client_key="c")
    )
    assert order.status == "accepted"
    assert client.calls[-1]["body"] == {
        "oder_date": "20261102",
        "orgl_oder_no": 77,
        "oder_cls_code": "01",
        "odqt": 2,
        "warn_cnfr_yn": "N",
    }
    assert client.count(*cancel) == 1


def test_us_cancel_of_an_unlisted_order_sends_nothing(unlocked, us_safety):
    cancel = ("POST", "/trading/v1/overseas/orders/cancel")
    broker, client, _ = make(
        {cancel: [fx("order_accept")], ("GET", US_HISTORY): [{"rsp_cd": "5820"}]}, safety=us_safety, **US
    )
    with pytest.raises(BrokerRejectedError):
        broker.cancel_order(
            CancelRequest(order_id="77", symbol="AAPL", quantity=2, market="US", venue="NASDAQ", client_key="c")
        )
    assert client.count(*cancel) == 0


def test_us_order_status_from_history():
    filled = copy.deepcopy(fx("history_us"))
    filled["data"][0].update(cntg_dqty="2.0000", cntg_crnc_prc="190.0", ncnt_odqt=0)
    broker, _, _ = make({("GET", US_HISTORY): [filled]}, **US)
    got = broker.status(Order(id="77", symbol="AAPL", status="accepted", market="US"))
    assert (got.status, got.filled, got.fill_price) == ("filled", 2, Decimal("190.0"))
    broker, _, _ = make({("GET", US_HISTORY): [fx("history_us")]}, **US)
    assert broker.status(Order(id="77", symbol="AAPL", status="accepted", market="US")).status == "open"


def test_us_cancelled_by_cancel_row():
    base = fx("history_us")["data"][0]
    cancel_row = {
        **base,
        "oder_no": 78,
        "orgl_oder_no": 77,
        "oder_rctf_cncl_cls_code": "2",
        "oder_dqty": "2.0000",
        "cntg_dqty": "0",
    }
    broker, _, _ = make({("GET", US_HISTORY): [{"rsp_cd": "5766", "tr_cont": "0", "data": [base, cancel_row]}]}, **US)
    got = broker.status(Order(id="77", symbol="AAPL", status="accepted", market="US"))
    assert (got.status, got.remaining) == ("cancelled", 0)


# ----------------------------------------------------------------------------- hygiene
def test_the_production_host_is_the_documented_one():
    assert module.MERITZ_API_BASE_URL == "https://openapi.imeritz.com:9443"


def test_default_client_targets_the_module_constant(monkeypatch):
    monkeypatch.setattr(module, "MERITZ_API_BASE_URL", "http://127.0.0.1:1")
    assert module._MeritzRestClient().base_url == "http://127.0.0.1:1"


# ----------------------------------------------------------------------------- the real HTTP client
class _Resp:
    status = 200

    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, raw):
    import io
    import urllib.error

    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(raw))


def test_the_real_client_reads_the_body_of_an_http_500(monkeypatch):
    def fake(request, *, timeout):
        assert request.full_url.startswith("http://127.0.0.1:1/market/v1/prices?")
        raise _http_error(500, b'{"rsp_cd": "0760", "rsp_msg": "x"}')

    monkeypatch.setattr(module, "urlopen", fake)
    status, body = module._MeritzRestClient("http://127.0.0.1:1").request(
        "GET", "/market/v1/prices", headers={}, params={"iscd": "005930"}
    )
    assert (status, body["rsp_cd"]) == (500, "0760")


def test_the_real_client_does_not_follow_a_redirect_and_a_non_json_body_is_invalid(monkeypatch):
    monkeypatch.setattr(module, "urlopen", lambda request, *, timeout: (_ for _ in ()).throw(_http_error(302, b"")))
    with pytest.raises(InvalidResponseError):
        module._MeritzRestClient("http://127.0.0.1:1").request("GET", "/x", headers={})


def test_the_real_client_sends_json_for_a_post(monkeypatch):
    seen = {}

    def fake(request, *, timeout):
        seen.update(method=request.get_method(), data=request.data, url=request.full_url)
        return _Resp(b'{"rsp_cd": "0001"}')

    monkeypatch.setattr(module, "urlopen", fake)
    status, body = module._MeritzRestClient("http://127.0.0.1:1").request("POST", "/p", headers={}, body={"a": 1})
    assert (status, body["rsp_cd"], seen["method"], json.loads(seen["data"])) == (200, "0001", "POST", {"a": 1})


def test_every_constructor_argument_is_keyword_only():
    import inspect

    parameters = list(inspect.signature(Meritz.__init__).parameters.values())[1:]
    assert parameters and all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)
    with pytest.raises(TypeError):
        Meritz("fixture-account", OPTIONS)  # type: ignore[misc]


def test_us_replacement_order_is_listed_and_the_superseded_parent_is_unknown(unlocked, us_safety):
    base = fx("history_us")["data"][0]
    child = {
        **base,
        "oder_no": 79,
        "orgl_oder_no": 77,
        "oder_rctf_cncl_cls_code": "1",
        "oder_dqty": "2.0000",
        "ovrs_stck_oder_prc": "189.0000",
    }
    page = {"rsp_cd": "5766", "tr_cont": "0", "data": [base, child]}
    cancel = ("POST", "/trading/v1/overseas/orders/cancel")
    broker, client, _ = make({cancel: [fx("order_accept")], ("GET", US_HISTORY): [page]}, safety=us_safety, **US)
    assert broker.status(Order(id="77", symbol="AAPL", status="accepted", market="US")).status == "unknown"
    assert broker.status(Order(id="79", symbol="AAPL", status="accepted", market="US")).status == "open"
    # a US change must name the latest modify: the stale number 77 sends nothing, 79 goes out
    with pytest.raises(BrokerRejectedError):
        broker.cancel_order(
            CancelRequest(order_id="77", symbol="AAPL", quantity=2, market="US", venue="NASDAQ", client_key="c1")
        )
    assert client.count(*cancel) == 0
    broker.cancel_order(
        CancelRequest(order_id="79", symbol="AAPL", quantity=2, market="US", venue="NASDAQ", client_key="c2")
    )
    assert client.count(*cancel) == 1 and client.calls[-1]["body"]["orgl_oder_no"] == 79


def test_connect_reads_the_deposit_once_and_returns_the_token_row():
    broker, client, _ = make({("GET", "/accounts/v1/me/deposit"): [fx("deposit")]})
    got = broker.connect()
    assert got["token"] == "fixture-access-token" and got["token_type"] == "Bearer"
    assert got["expires_dt"] == "20261109100000"  # NOW(01:00 UTC) + 7 days, as a KST wall clock
    assert client.count("GET", "/accounts/v1/me/deposit") == 1 and client.count("POST", "/oauth2/token") == 1


def test_connect_names_the_refusal_without_vendor_text():
    broker, _, _ = make({("GET", "/accounts/v1/me/deposit"): [(500, fx("error_no_permission"))]})
    with pytest.raises(BrokerRejectedError) as caught:
        broker.connect()
    assert caught.value.code == "0760" and "fixture" not in str(caught.value)
    module._TOKENS.clear()  # the first half cached a token for this app key
    broker, _, _ = make({("POST", "/oauth2/token"): [(403, fx("error_bad_key"))]})
    with pytest.raises(BrokerRejectedError) as caught:
        broker.connect()
    assert caught.value.code == "EGW00103"
