"""Kiwoom read half, offline (the order half is in test_kiwoom_order_contract.py).

`tests/fixtures/kiwoom/*.json` are the sample responses printed on the official portal pages (openapi.kiwoom.com,
read 2026-09-26 12:44 KST): au10001 has one comma restored, everything else is verbatim. They pin field names and
formats, not vendor behaviour, and several are visibly inconsistent (ka10001's values sit in the wrong fields), which
is exactly why they are here: what the spec cannot confirm must be refused. Values derived from them are marked
«synthetic». No live response was captured.
"""

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pykbroker.brokers import kiwoom as module
from pykbroker.brokers._common import BaseBroker
from pykbroker.brokers.kiwoom import KIWOOM_API_BASE_URL, KIWOOM_MOCK_BASE_URL, Kiwoom, _KiwoomRestClient
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.factory import BROKERS, create_broker
from pykbroker.models import OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

OFFICIAL = {
    path.stem: json.loads(path.read_text()) for path in (Path(__file__).parent / "fixtures" / "kiwoom").glob("*.json")
}
CODES = {"exchange": "KRX", "query_type": "1"}
OPTIONS = {"token": "fixture-token", "app_key": "fixture-app-key"}
END = {"cont-yn": "N"}
# synthetic: the official ka10001 field layout with mutually consistent values
BASIC = {**OFFICIAL["ka10001"], "cur_prc": "74100", "upl_pric": "96300", "lst_pric": "51900", "base_pric": "74100"}
# synthetic: the official ka10004 layout with a real-looking best bid/ask
BOOK = {**OFFICIAL["ka10004"], "buy_fpr_bid": "74000", "sel_fpr_bid": "74100"}
RESPONSES = {"ka10001": BASIC, "ka10004": BOOK, "kt00001": OFFICIAL["kt00001"], "kt00018": OFFICIAL["kt00018"]}
PRICE_SIGNS = ["-74100", " -74100", "-0"]


class FakeClient:
    def __init__(self, base_url, responses=None):
        self.base_url, self.calls = base_url, []
        # a list is a page sequence for one TR; a dict is a single page whose headers say «no more»
        self.responses = {k: list(v) if isinstance(v, list) else v for k, v in dict(responses or RESPONSES).items()}

    def post(self, path, *, headers, json):
        self.calls.append({"path": path, "headers": headers, "json": json})
        response = self.responses[headers.get("api-id", path)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            return response.pop(0)
        return response, dict(END)


def make(responses=None, *, options=None, codes=CODES, **kwargs):
    made = []

    def factory(*, base_url):
        made.append(FakeClient(base_url, responses))
        return made[0]

    limiter = RateLimiter({tr: 1000 for tr in module.KIWOOM_TR_RATE_LIMITS})
    broker = create_broker(
        "kiwoom",
        account="fixture-account",
        client_factory=factory,
        client_options=OPTIONS if options is None else options,
        codes=codes,
        rate_limiter=limiter,
        **kwargs,
    )
    return broker, made


def with_row(**changes):
    row = {**OFFICIAL["kt00018"]["acnt_evlt_remn_indv_tot"][0], **changes}
    return {**OFFICIAL["kt00018"], "acnt_evlt_remn_indv_tot": [row]}


# --- au10001: issuance ---


def issue(responses, **kwargs):
    made = []

    def factory(*, base_url):
        made.append(FakeClient(base_url, {"/oauth2/token": responses}))
        return made[0]

    args = {"app_key": "fixture-app-key", "secret_key": "fixture-secret-key", "client_factory": factory, **kwargs}
    return Kiwoom.issue_token(**args), made


def test_issue_token_request_and_verbatim_response():
    issued, made = issue(OFFICIAL["au10001"])
    (call,) = made[0].calls
    assert call["path"] == "/oauth2/token" and made[0].base_url == KIWOOM_API_BASE_URL
    assert call["json"] == {
        "grant_type": "client_credentials",
        "appkey": "fixture-app-key",
        "secretkey": "fixture-secret-key",
    }
    # the au10001 page has no header table: nothing but the content type is sent
    assert call["headers"] == {"content-type": "application/json; charset=utf-8"}
    assert issued == {"token": OFFICIAL["au10001"]["token"], "token_type": "bearer", "expires_dt": "20241107083713"}
    assert "secret" not in json.dumps(issued)


def test_issue_token_host_follows_virtual_only():
    assert issue(OFFICIAL["au10001"], virtual=True)[1][0].base_url == KIWOOM_MOCK_BASE_URL
    with pytest.raises(ConfigurationError):
        issue(OFFICIAL["au10001"], virtual="yes")


def test_issued_token_feeds_the_broker_unchanged():
    issued, _ = issue(OFFICIAL["au10001"])
    broker, made = make(
        options={**issued, "app_key": "fixture-app-key"}, clock=lambda: datetime(2024, 11, 6, tzinfo=UTC)
    )
    broker.get_quote("005930")
    assert made[0].calls[0]["headers"]["authorization"] == f"Bearer {issued['token']}"


@pytest.mark.parametrize(
    "body",
    [
        {**OFFICIAL["au10001"], "token": ""},
        {**OFFICIAL["au10001"], "token": 5},
        {**OFFICIAL["au10001"], "expires_dt": "2024-11-07"},
        {**OFFICIAL["au10001"], "expires_dt": "20241340083713"},
        {k: v for k, v in OFFICIAL["au10001"].items() if k != "token_type"},
        {k: v for k, v in OFFICIAL["au10001"].items() if k != "return_code"},
    ],
)
def test_malformed_issuance_is_an_invalid_response(body):
    with pytest.raises(InvalidResponseError):
        issue(body)


def test_rejected_or_failed_issuance_leaks_no_secret():
    with pytest.raises(BrokerRejectedError) as rejected:
        issue({"return_code": 8001, "return_msg": "fixture-secret-key rejected"})
    assert "fixture-secret-key" not in str(rejected.value)
    with pytest.raises(TransportError) as failed:
        issue(OSError("connect fixture-secret-key failed"))
    assert "fixture-secret-key" not in str(failed.value) and failed.value.__cause__ is None
    for bad in ({"app_key": ""}, {"secret_key": " "}):
        with pytest.raises(ConfigurationError):
            issue(OFFICIAL["au10001"], **bad)


# --- stored token: reuse and conservative expiry ---


def test_stored_token_is_reused_and_never_reissued():
    broker, made = make()
    broker.get_quote("005930"), broker.get_positions(), broker.get_quote("005930")
    assert {c["headers"]["authorization"] for c in made[0].calls} == {"Bearer fixture-token"}
    assert {c["path"] for c in made[0].calls} == {"/api/dostk/stkinfo", "/api/dostk/mrkcond", "/api/dostk/acnt"}
    assert len(made) == 1


@pytest.mark.parametrize(
    "token,token_type,expected",
    [("abc", None, "Bearer abc"), ("Bearer abc", None, "Bearer abc"), ("abc", "bearer", "Bearer abc")],
)
def test_authorization_header_forms(token, token_type, expected):
    options = {**OPTIONS, "token": token, **({"token_type": token_type} if token_type else {})}
    broker, made = make(options=options)
    broker.get_quote("005930")
    assert made[0].calls[0]["headers"]["authorization"] == expected


def test_only_the_verified_token_type_is_sent():
    with pytest.raises(ConfigurationError):
        make(options={**OPTIONS, "token_type": "Basic"})


@pytest.mark.parametrize(
    "now,refused",
    [
        # expires_dt 20241107083713 read as KST is 2024-11-06 23:37:13 UTC; the reading as UTC would be 9h later
        (datetime(2024, 11, 6, 23, 31, tzinfo=UTC), False),
        (datetime(2024, 11, 6, 23, 33, tzinfo=UTC), True),  # inside the 5-minute margin
        (datetime(2024, 11, 6, 23, 37, 13, tzinfo=UTC), True),
        (datetime(2024, 11, 7, 3, 0, tzinfo=UTC), True),  # fresh under a UTC reading, stale under the KST one
    ],
)
def test_expiry_is_judged_early_and_before_any_request(now, refused):
    broker, made = make(options={**OPTIONS, "expires_dt": "20241107083713"}, clock=lambda: now)
    if refused:
        with pytest.raises(ConfigurationError):
            broker.get_quote("005930")
        assert made[0].calls == []
    else:
        broker.get_quote("005930")


@pytest.mark.parametrize("value", ["", "2024-11-07", "20241107", 20241107083713, "20241340083713"])
def test_unreadable_expiry_is_a_configuration_error(value):
    with pytest.raises(ConfigurationError):
        make(options={**OPTIONS, "expires_dt": value})


def test_without_expiry_the_server_arbitrates():
    broker, _ = make(clock=lambda: datetime(2100, 1, 1, tzinfo=UTC))
    broker.get_quote("005930")


# --- ka10001 / ka10004: quote ---


def test_quote_request_and_parse():
    broker, made = make()
    quote = broker.get_quote("A005930")
    assert quote.symbol == "005930" and quote.last == Decimal("74100")
    assert quote.bid == Decimal("74000") and quote.ask == Decimal("74100")
    basic, book = made[0].calls
    assert basic["path"] == "/api/dostk/stkinfo" and basic["json"] == {"stk_cd": "005930"}
    assert book["path"] == "/api/dostk/mrkcond" and book["json"] == {"stk_cd": "005930"}
    assert basic["headers"] == {
        "content-type": "application/json; charset=utf-8",
        "authorization": "Bearer fixture-token",
        "api-id": "ka10001",
    }
    assert book["headers"]["api-id"] == "ka10004"


def test_official_after_hours_book_is_unknown_not_a_zero_price():
    quote = make({**RESPONSES, "ka10004": OFFICIAL["ka10004"]})[0].get_quote("005930")
    assert quote.bid is None and quote.ask is None and quote.last == Decimal("74100")


def test_crossed_or_partial_book_is_reported_unknown():
    crossed = {**BOOK, "buy_fpr_bid": "74200", "sel_fpr_bid": "74100"}
    quote = make({**RESPONSES, "ka10004": crossed})[0].get_quote("005930")
    assert quote.bid is None and quote.ask is None
    partial = {**BOOK, "buy_fpr_bid": "0"}
    quote = make({**RESPONSES, "ka10004": partial})[0].get_quote("005930")
    assert quote.bid is None and quote.ask == Decimal("74100")


def test_official_ka10001_sample_is_refused_because_its_fields_are_misaligned():
    broker, _ = make({**RESPONSES, "ka10001": OFFICIAL["ka10001"]})
    with pytest.raises(InvalidResponseError):  # cur_prc "0.00" is not a price
        broker.get_quote("005930")
    with pytest.raises(SpecUnverifiedError):  # lst_pric "-47.41" carries an unverified sign
        broker.get_rules("005930")


def test_plus_sign_on_a_price_is_its_magnitude():
    broker, _ = make(
        {**RESPONSES, "ka10001": {**BASIC, "cur_prc": "+74100"}, "ka10004": {**BOOK, "sel_fpr_bid": "+74100"}}
    )
    quote = broker.get_quote("005930")
    assert quote.last == Decimal("74100") and quote.ask == Decimal("74100")


@pytest.mark.parametrize("sign", PRICE_SIGNS)
@pytest.mark.parametrize(
    "tr,field,read",
    [
        ("ka10001", "cur_prc", "get_quote"),
        ("ka10004", "buy_fpr_bid", "get_quote"),
        ("ka10004", "sel_fpr_bid", "get_quote"),
        ("ka10001", "cur_prc", "get_rules"),
        ("ka10001", "upl_pric", "get_rules"),
        ("ka10001", "lst_pric", "get_rules"),
        ("ka10001", "base_pric", "get_rules"),
    ],
)
def test_minus_sign_on_any_price_is_refused_as_unverified(tr, field, read, sign):
    broker, _ = make({**RESPONSES, tr: {**RESPONSES[tr], field: sign}})
    with pytest.raises(SpecUnverifiedError):
        getattr(broker, read)("005930")


@pytest.mark.parametrize("field", ["pur_pric", "cur_prc"])
def test_minus_sign_on_a_holding_price_is_refused_as_unverified(field):
    broker, _ = make({**RESPONSES, "kt00018": with_row(**{field: "-000000059000"})})
    with pytest.raises(SpecUnverifiedError):
        broker.get_positions()


@pytest.mark.parametrize("field", ["pur_pric", "cur_prc"])
def test_plus_sign_on_a_holding_price_preserves_its_magnitude(field):
    broker, _ = make({**RESPONSES, "kt00018": with_row(**{field: "+000000059000"})})
    (position,) = broker.get_positions()
    assert getattr(position, "average_price" if field == "pur_pric" else "last") == Decimal("59000")


@pytest.mark.parametrize(
    "body",
    [
        {"return_code": 0},
        {"return_code": 0, "cur_prc": ""},
        {"return_code": 0, "cur_prc": "0"},
        {"cur_prc": "74100"},
        "[]",
    ],
)
def test_malformed_quote_is_an_invalid_response(body):
    broker, _ = make({**RESPONSES, "ka10001": body})
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")


# --- ka10001: band ---


def test_rules_carry_the_venue_band_from_the_named_fields():
    broker, made = make(halt_provider=lambda symbol: False)
    rules = broker.get_rules("005930")
    assert (rules.limit_up, rules.limit_down, rules.limits_source) == (Decimal("96300"), Decimal("51900"), "venue")
    assert rules.tick == Decimal("100") and rules.halted is False
    assert [c["headers"]["api-id"] for c in made[0].calls] == ["ka10001"]  # the band needs no order book


@pytest.mark.parametrize("missing", [{"upl_pric": "0"}, {"lst_pric": ""}, {"upl_pric": "0", "lst_pric": "0"}])
def test_unstated_or_half_band_stays_unknown_and_is_not_derived(missing):
    rules = make({**RESPONSES, "ka10001": {**BASIC, **missing}})[0].get_rules("005930")
    assert rules.limit_up is None and rules.limit_down is None and rules.limits_source is None


@pytest.mark.parametrize(
    "changes",
    [
        {"upl_pric": "51900", "lst_pric": "96300"},  # inverted
        {"cur_prc": "96400"},  # last outside its own band
        {"base_pric": "20231024"},  # 기준가 outside the band (the official sample's shape)
    ],
)
def test_a_band_that_contradicts_the_prices_is_a_mapping_fault(changes):
    broker, _ = make({**RESPONSES, "ka10001": {**BASIC, **changes}})
    with pytest.raises(InvalidResponseError):
        broker.get_rules("005930")


def test_closed_broker_refuses_rules_and_reads():
    broker, _ = make()
    broker.close()
    for read in (broker.get_quote, broker.get_rules):
        with pytest.raises(ConfigurationError):
            read("005930")


# --- kt00001: order-available cash ---


def test_cash_request_and_the_official_sample():
    broker, made = make()
    assert broker.available_cash() == Decimal("17534")  # ord_alow_amt 85341 capped by entr 17534
    (call,) = made[0].calls
    assert call["path"] == "/api/dostk/acnt" and call["headers"]["api-id"] == "kt00001"
    assert call["json"] == {"qry_tp": "2"}


def test_cash_is_ord_alow_amt_when_it_is_the_smaller():
    body = {**OFFICIAL["kt00001"], "ord_alow_amt": "000000000000500"}
    assert make({**RESPONSES, "kt00001": body})[0].available_cash() == Decimal("500")


def test_cash_read_needs_no_position_codes_and_makes_one_call():
    broker, made = make(codes={})
    broker.available_cash()
    assert [c["headers"]["api-id"] for c in made[0].calls] == ["kt00001"]


def test_estimated_deposit_asset_is_never_cash():
    assert OFFICIAL["kt00018"]["prsm_dpst_aset_amt"] == "000001012632507"
    balance = make()[0].get_balance()
    assert balance.cash_available == Decimal("17534") != Decimal("1012632507")
    assert balance.position_value == Decimal("857000")  # the two official rows: 177000 + 680000
    assert balance.total == Decimal("874534")


@pytest.mark.parametrize("drop", ["ord_alow_amt", "entr"])
def test_cash_without_either_field_is_invalid(drop):
    body = {k: v for k, v in OFFICIAL["kt00001"].items() if k != drop}
    with pytest.raises(InvalidResponseError):
        make({**RESPONSES, "kt00001": body})[0].available_cash()


@pytest.mark.parametrize("value", ["-000000000085341", "", "abc"])
def test_negative_or_unreadable_cash_is_invalid(value):
    with pytest.raises(InvalidResponseError):
        make({**RESPONSES, "kt00001": {**OFFICIAL["kt00001"], "ord_alow_amt": value}})[0].available_cash()


# --- kt00018: positions and continuation ---


def test_positions_request_and_the_official_sample():
    broker, made = make()
    first, second = broker.get_positions()
    assert (first.symbol, first.quantity, first.sellable) == ("005930", 3, 3)
    assert first.average_price == Decimal("124500") and first.last == Decimal("59000")
    assert first.market_value == Decimal("177000") and second.quantity == 8
    (call,) = made[0].calls
    assert call["path"] == "/api/dostk/acnt" and call["headers"]["api-id"] == "kt00018"
    assert call["json"] == {"dmst_stex_tp": "KRX", "qry_tp": "1"}
    assert "cont-yn" not in call["headers"] and "next-key" not in call["headers"]


def test_available_qty_reads_the_sellable_column():
    broker, _ = make({**RESPONSES, "kt00018": with_row(trde_able_qty="000000000000002")})
    assert broker.available_qty("005930") == 2 and broker.available_qty("000660") == 0


def page(rows, cont, key=None):
    headers = {"cont-yn": cont, **({"next-key": key} if key else {})}
    return {**OFFICIAL["kt00018"], "acnt_evlt_remn_indv_tot": rows}, headers


def test_continuation_headers_are_echoed_and_pages_are_joined():
    rows = OFFICIAL["kt00018"]["acnt_evlt_remn_indv_tot"]
    broker, made = make(
        {**RESPONSES, "kt00018": [page(rows[:1], "Y", "K1"), page(rows[1:], "Y", "K2"), page(rows[:1], "N")]}
    )
    assert len(broker.get_positions()) == 3
    first, second, third = made[0].calls
    assert "cont-yn" not in first["headers"] and "next-key" not in first["headers"]
    assert (second["headers"]["cont-yn"], second["headers"]["next-key"]) == ("Y", "K1")
    assert (third["headers"]["cont-yn"], third["headers"]["next-key"]) == ("Y", "K2")
    assert first["json"] == second["json"] == third["json"]


def test_response_header_names_are_case_insensitive():
    rows = OFFICIAL["kt00018"]["acnt_evlt_remn_indv_tot"]
    pages = [(page(rows[:1], "Y")[0], {"Cont-Yn": "Y", "Next-Key": "K1"}), (page(rows[1:], "N")[0], {"CONT-YN": "N"})]
    broker, made = make({**RESPONSES, "kt00018": pages})
    assert len(broker.get_positions()) == 2 and made[0].calls[1]["headers"]["next-key"] == "K1"


@pytest.mark.parametrize(
    "pages",
    [
        [({**OFFICIAL["kt00018"]}, {})],  # no continuation header: the end cannot be shown
        [({**OFFICIAL["kt00018"]}, {"cont-yn": "?"})],
        [({**OFFICIAL["kt00018"]}, {"cont-yn": "Y"})],  # more, but no key
        [({**OFFICIAL["kt00018"]}, {"cont-yn": "Y", "next-key": ""})],
        [
            ({**OFFICIAL["kt00018"]}, {"cont-yn": "Y", "next-key": "K"}),
            ({**OFFICIAL["kt00018"]}, {"cont-yn": "Y", "next-key": "K"}),  # the same key twice: a loop
        ],
    ],
)
def test_a_list_that_may_be_truncated_is_refused(pages):
    with pytest.raises(InvalidResponseError):
        make({**RESPONSES, "kt00018": pages})[0].get_positions()


def test_endless_pages_are_cut_off_by_refusal_not_by_a_partial_list():
    broker, made = make()
    made[0].responses["kt00018"] = [
        ({**OFFICIAL["kt00018"]}, {"cont-yn": "Y", "next-key": f"K{n}"}) for n in range(module.KIWOOM_MAX_PAGES + 5)
    ]
    with pytest.raises(InvalidResponseError):
        broker.get_positions()
    assert len(made[0].calls) == module.KIWOOM_MAX_PAGES


def test_position_codes_are_checked_against_the_spec_enumerations():
    assert make(codes={"query_type": "2"})[0].get_positions()  # exchange defaults to KRX
    for codes in ({}, {"query_type": "3"}, {"query_type": "1", "exchange": "NXT"}, {"query_type": 1}):
        broker, made = make(codes=codes)
        with pytest.raises(ConfigurationError):
            broker.get_positions()
        with pytest.raises(ConfigurationError):
            broker.get_balance()
        assert made[0].calls == []


def test_positions_without_the_holdings_list_or_with_oversold_row_are_invalid():
    broker, _ = make({**RESPONSES, "kt00018": {"return_code": 0, "tot_evlt_amt": "0"}})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()
    broker, _ = make({**RESPONSES, "kt00018": with_row(trde_able_qty="000000000000009")})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


# --- return_code, transport, host, limits ---


@pytest.mark.parametrize("code", [1, 8005, 1700, "8005", "-1", "0", True, None])
def test_any_body_code_but_integer_zero_is_a_rejection(code):
    body = {**BASIC, "return_code": code, "return_msg": "secret detail"}
    with pytest.raises(BrokerRejectedError) as raised:
        make({**RESPONSES, "ka10001": body})[0].get_quote("005930")
    assert "secret detail" not in str(raised.value)


def test_missing_return_code_is_invalid_not_success():
    body = {k: v for k, v in BASIC.items() if k != "return_code"}
    with pytest.raises(InvalidResponseError):
        make({**RESPONSES, "ka10001": body})[0].get_quote("005930")


def test_transport_failures_are_classified_and_leak_nothing():
    broker, _ = make({**RESPONSES, "ka10001": OSError("connect to fixture-token failed")})
    with pytest.raises(TransportError) as raised:
        broker.get_quote("005930")
    assert "fixture-token" not in str(raised.value) and raised.value.__cause__ is None


def test_a_client_that_drops_the_headers_is_refused():
    broker, made = make()
    made[0].post = lambda path, *, headers, json: OFFICIAL["ka10001"]
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")


@pytest.mark.parametrize("virtual,host", [(False, KIWOOM_API_BASE_URL), (True, KIWOOM_MOCK_BASE_URL)])
def test_host_is_chosen_by_virtual_only(virtual, host):
    broker, made = make(virtual=virtual)
    assert made[0].base_url == host
    assert broker.capabilities().virtual is virtual and "mockapi" in KIWOOM_MOCK_BASE_URL


def test_default_client_is_pinned_to_the_mock_host_when_virtual():
    broker = Kiwoom(account="fixture-account", client_options=OPTIONS, virtual=True)
    assert broker._client.base_url == KIWOOM_MOCK_BASE_URL != KIWOOM_API_BASE_URL


@pytest.mark.parametrize("key", ["base_url", "host", "url", "app_secret", "secret_key", "secretkey", "virtual_account"])
def test_options_cannot_move_the_host_or_smuggle_a_secret(key):
    with pytest.raises(ConfigurationError):
        make(options={**OPTIONS, key: "https://elsewhere.example"})


@pytest.mark.parametrize("options", [{}, {"app_key": "k"}, {"token": "t"}, {"token": "", "app_key": "k"}])
def test_stored_token_and_app_key_are_required(options):
    with pytest.raises(ConfigurationError):
        make(options=options)


def test_limiter_covers_every_read_tr_within_the_documented_budget():
    read_trs = ("ka10001", "ka10004", "kt00001", "kt00018", "ka10075", "kt00007")
    assert set(module.KIWOOM_TR_RATE_LIMITS) == {*read_trs, "kt10000", "kt10001", "kt10003"}
    assert max(module.KIWOOM_TR_RATE_LIMITS.values()) <= 1  # mock: one call per TR per second
    # real: 5 read calls/s and 5 order calls/s, even if each is shared by the account
    assert sum(module.KIWOOM_TR_RATE_LIMITS[tr] for tr in read_trs) <= 5
    assert sum(module.KIWOOM_TR_RATE_LIMITS[tr] for tr in ("kt10000", "kt10001", "kt10003")) <= 5
    broker = Kiwoom(account="fixture-account", client_options=OPTIONS, codes=CODES)
    for tr in module.KIWOOM_TR_RATE_LIMITS:
        broker._limiter.wait(tr)


def test_limiter_is_awaited_before_each_post():
    events = []

    class Limiter:
        def wait(self, tr):
            events.append(("wait", tr))

    broker, made = make()
    broker._limiter = Limiter()
    made[0].post = lambda path, *, headers, json: events.append(("post", headers["api-id"])) or (BASIC, dict(END))
    broker.get_rules("005930")
    assert events == [("wait", "ka10001"), ("post", "ka10001")]


# --- boundaries ---


def test_market_and_after_hours_orders_are_refused_before_any_request():
    broker, made = make()
    for order_type in ("market", "close"):
        request = OrderRequest(symbol="005930", side="buy", quantity=1, order_type=order_type, client_key="k")
        with pytest.raises(NotSupportedError):
            broker.place_order(request)
    assert made[0].calls == []


def test_capabilities_admit_kr_limit_orders_and_single_cancel_only():
    caps = make()[0].capabilities()
    assert caps.venues == ("KRX",) and caps.markets == ("KR",) and caps.supports_virtual_account is True
    assert caps.supports_limit_order and caps.supports_single_order_cancel and caps.supports_order_history
    assert not (caps.supports_market_order or caps.supports_nxt or caps.realtime_open_orders)


def test_us_market_and_wrong_market_quote_are_refused():
    with pytest.raises(NotSupportedError):
        make(market="US")
    with pytest.raises(NotSupportedError):
        make()[0].get_quote("AAPL", market="US")


def test_it_is_the_registered_broker():
    assert issubclass(Kiwoom, BaseBroker) and BROKERS["kiwoom"] is Kiwoom


def test_the_legacy_slips_are_not_repeated():
    source = inspect.getsource(module)
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    # cash is never prsm_dpst_aset_amt, no kt00010 call (its trde_tp is 1 sell / 2 buy), no US TR
    assert 'row["prsm_dpst_aset_amt"]' not in code and "kt00010" not in source and "ust2" not in source


class _Response:
    headers = {"Cont-Yn": "Y", "Next-Key": "K1"}

    def __init__(self, body=b'{"return_code": 0}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_rest_client_posts_json_and_returns_the_body_with_lowercase_headers(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen.update(url=request.full_url, body=json.loads(request.data), method=request.method, timeout=timeout)
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    client = _KiwoomRestClient("https://fixture.invalid/", timeout=3)
    body, headers = client.post("/p", headers={"api-id": "x"}, json={"a": "한"})
    assert body == {"return_code": 0} and headers == {"cont-yn": "Y", "next-key": "K1"}
    assert seen == {"url": "https://fixture.invalid/p", "body": {"a": "한"}, "method": "POST", "timeout": 3}
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, timeout: _Response(b"[1]"))
    with pytest.raises(ValueError):
        client.post("/p", headers={}, json={})


def test_an_http_error_status_is_a_transport_failure_not_a_parsed_body(monkeypatch):
    import urllib.error

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "fixture-token", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    broker = Kiwoom(account="fixture-account", client_options=OPTIONS, codes=CODES)
    with pytest.raises(TransportError) as raised:
        broker.get_quote("005930")
    assert "fixture-token" not in str(raised.value)
