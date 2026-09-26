"""Kiwoom read half, offline. Fixture values are synthetic (trader test_kiwoom_broker_adapter.py)
and enum codes are placeholders: this proves request assembly and parsing, not vendor behaviour."""

import json
from decimal import Decimal

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
from pykbroker.factory import BROKERS
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

CODES = {"exchange": "fixture-exchange", "query_type": "fixture-query"}
OPTIONS = {"token": "fixture-token", "app_key": "fixture-app-key"}
RESPONSES = {
    "kt00018": {
        "return_code": 0,
        "prsm_dpst_aset_amt": "10000",
        "tot_evlt_amt": "7200",
        "acnt_evlt_remn_indv_tot": [
            {
                "stk_cd": "005930",
                "stk_nm": "fixture-domestic",
                "rmnd_qty": "3",
                "trde_able_qty": "2",
                "pur_pric": "2200",
                "cur_prc": "2400",
                "evlt_amt": "7200",
            }
        ],
    },
    "ka10001": {"return_code": 0, "stk_cd": "005930", "cur_prc": "2500", "open_pric": "2300"},
}


class FakeClient:
    def __init__(self, base_url, responses=None):
        self.base_url, self.calls, self.responses = base_url, [], dict(responses or RESPONSES)

    def post(self, path, *, headers, json):
        self.calls.append({"path": path, "headers": headers, "json": json})
        response = self.responses[headers["api-id"]]
        if isinstance(response, Exception):
            raise response
        return response


def make(responses=None, *, options=None, codes=CODES, **kwargs):
    made = []

    def factory(*, base_url):
        made.append(FakeClient(base_url, responses))
        return made[0]

    limiter = RateLimiter({"ka10001": 1000, "kt00018": 1000})
    broker = Kiwoom(
        account="fixture-account",
        client_factory=factory,
        client_options=OPTIONS if options is None else options,
        codes=codes,
        rate_limiter=limiter,
        **kwargs,
    )
    return broker, made


def test_quote_request_and_parse():
    broker, made = make()
    quote = broker.get_quote("A005930")
    assert quote.symbol == "005930" and quote.last == Decimal("2500") and quote.bid is None and quote.ask is None
    (call,) = made[0].calls
    assert call["path"] == "/api/dostk/stkinfo" and call["json"] == {"stk_cd": "005930"}
    assert call["headers"] == {
        "content-type": "application/json; charset=utf-8",
        "authorization": "Bearer fixture-token",
        "api-id": "ka10001",
    }


def test_positions_request_and_parse():
    broker, made = make()
    (position,) = broker.get_positions()
    assert (position.symbol, position.quantity, position.sellable) == ("005930", 3, 2)
    assert position.average_price == Decimal("2200") and position.market_value == Decimal("7200")
    (call,) = made[0].calls
    assert call["path"] == "/api/dostk/acnt" and call["headers"]["api-id"] == "kt00018"
    assert call["json"] == {"dmst_stex_tp": "fixture-exchange", "qry_tp": "fixture-query"}


def test_available_qty_reads_the_sellable_column():
    broker, _ = make()
    assert broker.available_qty("005930") == 2 and broker.available_qty("000660") == 0


def test_rules_use_price_and_leave_the_band_unknown():
    broker, _ = make(halt_provider=lambda symbol: False)
    rules = broker.get_rules("005930")
    assert rules.tick == Decimal("5") and rules.halted is False
    assert rules.limit_up is None and rules.limit_down is None and rules.limits_source is None


def test_cash_is_locked_until_the_order_cash_tr_is_verified():
    broker, made = make()
    for read in (broker.get_balance, broker.available_cash):
        with pytest.raises(SpecUnverifiedError):
            read()
    assert made[0].calls == []


def test_stored_token_is_reused_and_never_reissued():
    broker, made = make()
    broker.get_quote("005930"), broker.get_positions(), broker.get_quote("005930")
    assert {c["headers"]["authorization"] for c in made[0].calls} == {"Bearer fixture-token"}
    assert {c["headers"]["api-id"] for c in made[0].calls} == {"ka10001", "kt00018"}
    assert len(made) == 1


@pytest.mark.parametrize(
    "token,token_type,expected",
    [("abc", None, "Bearer abc"), ("Bearer abc", None, "Bearer abc"), ("abc", "bearer", "bearer abc")],
)
def test_authorization_header_forms(token, token_type, expected):
    options = {**OPTIONS, "token": token, **({"token_type": token_type} if token_type else {})}
    broker, made = make(options=options)
    broker.get_quote("005930")
    assert made[0].calls[0]["headers"]["authorization"] == expected


@pytest.mark.parametrize("virtual,host", [(False, KIWOOM_API_BASE_URL), (True, KIWOOM_MOCK_BASE_URL)])
def test_host_is_chosen_by_virtual_only(virtual, host):
    broker, made = make(virtual=virtual)
    assert made[0].base_url == host
    assert broker.capabilities().virtual is virtual and "mockapi" in KIWOOM_MOCK_BASE_URL


def test_default_client_is_pinned_to_the_mock_host_when_virtual():
    broker = Kiwoom(account="fixture-account", client_options=OPTIONS, virtual=True)
    assert broker._client.base_url == KIWOOM_MOCK_BASE_URL != KIWOOM_API_BASE_URL


@pytest.mark.parametrize("key", ["base_url", "host", "url", "app_secret", "virtual_account"])
def test_options_cannot_move_the_host_or_smuggle_a_secret(key):
    with pytest.raises(ConfigurationError):
        make(options={**OPTIONS, key: "https://elsewhere.example"})


@pytest.mark.parametrize("options", [{}, {"app_key": "k"}, {"token": "t"}, {"token": "", "app_key": "k"}])
def test_stored_token_and_app_key_are_required(options):
    with pytest.raises(ConfigurationError):
        make(options=options)


def test_unsupplied_enum_codes_refuse_the_positions_call():
    broker, made = make(codes={"exchange": "fixture-exchange"})
    with pytest.raises(ConfigurationError):
        broker.get_positions()
    assert made[0].calls == []


def test_limiter_scopes_to_the_credential_and_covers_every_read_tr():
    broker = Kiwoom(account="fixture-account", client_options=OPTIONS, codes=CODES)
    assert module.KIWOOM_TR_RATE_LIMITS == {"kt00018": 1, "ka10001": 5}
    broker._limiter.wait("ka10001"), broker._limiter.wait("kt00018")


def test_limiter_is_awaited_before_each_post():
    events = []

    class Limiter:
        def wait(self, tr):
            events.append(("wait", tr))

    broker, made = make()
    broker._limiter = Limiter()
    made[0].post = lambda path, *, headers, json: events.append(("post", headers["api-id"])) or RESPONSES["ka10001"]
    broker.get_quote("005930")
    assert events == [("wait", "ka10001"), ("post", "ka10001")]


@pytest.mark.parametrize("code", [1, "8005", "-1"])
def test_nonzero_return_code_is_a_rejection(code):
    broker, _ = make({"ka10001": {"return_code": code, "return_msg": "secret detail", "cur_prc": "2500"}})
    with pytest.raises(BrokerRejectedError) as raised:
        broker.get_quote("005930")
    assert "secret detail" not in str(raised.value)


@pytest.mark.parametrize("code", [0, "0"])
def test_zero_return_code_is_accepted(code):
    broker, _ = make({"ka10001": {"return_code": code, "cur_prc": "2500"}})
    assert broker.get_quote("005930").last == Decimal("2500")


@pytest.mark.parametrize(
    "body",
    [
        {"return_code": 0},
        {"return_code": 0, "cur_prc": ""},
        {"return_code": 0, "cur_prc": "-2500"},
        {"cur_prc": "0"},
        [],
    ],
)
def test_malformed_quote_is_an_invalid_response(body):
    broker, _ = make({"ka10001": body})
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")


def test_positions_without_the_holdings_list_or_with_oversold_row_are_invalid():
    broker, _ = make({"kt00018": {"return_code": 0, "tot_evlt_amt": "0"}})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()
    row = {**RESPONSES["kt00018"]["acnt_evlt_remn_indv_tot"][0], "trde_able_qty": "9"}
    broker, _ = make({"kt00018": {**RESPONSES["kt00018"], "acnt_evlt_remn_indv_tot": [row]}})
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_transport_failures_are_classified_and_leak_nothing():
    broker, _ = make({"ka10001": OSError("connect to fixture-token failed")})
    with pytest.raises(TransportError) as raised:
        broker.get_quote("005930")
    assert "fixture-token" not in str(raised.value) and raised.value.__cause__ is None


def test_orders_and_cancels_are_refused_before_any_request():
    broker, made = make()
    with pytest.raises(NotSupportedError):
        broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1, price="2500", client_key="k"))
    with pytest.raises(NotSupportedError):
        broker.cancel_order(CancelRequest(symbol="005930", order_id="1", quantity=1, client_key="k2"))
    with pytest.raises(NotSupportedError):
        broker.list_orders()
    assert made[0].calls == []


def test_capabilities_admit_read_only_kr():
    caps = make()[0].capabilities()
    assert caps.venues == ("KRX",) and caps.markets == ("KR",) and caps.supports_virtual_account is True
    assert not (caps.supports_limit_order or caps.supports_market_order or caps.supports_single_order_cancel)


def test_us_market_and_wrong_market_quote_are_refused():
    with pytest.raises(NotSupportedError):
        make(market="US")
    with pytest.raises(NotSupportedError):
        make()[0].get_quote("AAPL", market="US")


def test_closed_broker_refuses_reads():
    broker, _ = make()
    broker.close()
    with pytest.raises(ConfigurationError):
        broker.get_quote("005930")


def test_it_is_a_broker_but_stays_out_of_the_factory():
    assert issubclass(Kiwoom, BaseBroker) and "kiwoom" not in BROKERS


def test_rest_client_posts_json_to_its_base_url(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"return_code": 0}'

    def urlopen(request, timeout):
        seen.update(url=request.full_url, body=json.loads(request.data), method=request.method, timeout=timeout)
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    client = _KiwoomRestClient("https://fixture.invalid/", timeout=3)
    assert client.post("/p", headers={"api-id": "x"}, json={"a": "한"}) == {"return_code": 0}
    assert seen == {"url": "https://fixture.invalid/p", "body": {"a": "한"}, "method": "POST", "timeout": 3}
    monkeypatch.setattr(Response, "read", lambda self: b"[1]")
    with pytest.raises(ValueError):
        client.post("/p", headers={}, json={})
