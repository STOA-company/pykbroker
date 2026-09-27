"""NH PLUG read contract from official common/krstock OpenAPI, with fake HTTP only."""

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pykbroker.brokers.nh import NH, NH_API_BASE_URL, NH_MOCK_BASE_URL
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, InvalidResponseError, TransportError
from pykbroker.factory import BROKERS

NOW = datetime(2026, 9, 27, tzinfo=UTC)
KEY = "fixture-key"
SECRET = "fixture-secret&=한글"
TOKEN = "fixture-token"
BALANCE = {
    "Output_0": {"dca": 50000, "orr_pbl_amt": "40000", "tot_eal_amt": 30000},
    "Output_1": [
        {
            "iem_cd": "005930",
            "iem_nm": "Samsung",
            "itg_bnc_qty": 2,
            "phs_pr": 14000,
            "now_pr": 15000,
            "eal_amt": 30000,
        }
    ],
    "message": {"usr_msg": "조회 완료"},
}


class FakeTransport:
    def __init__(self, *, base_url, responses, calls):
        self.base_url, self.responses, self.calls = base_url, responses, calls

    def post(self, path, *, headers, data):
        self.calls.append((self.base_url, path, dict(headers), data))
        response = self.responses[path]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make(responses=None, *, options=None, virtual=False, cache=None, venue="KRX"):
    calls = []
    replies = responses or {}

    def factory(*, base_url):
        return FakeTransport(base_url=base_url, responses=replies, calls=calls)

    broker = NH(
        account="12345678901",
        client_options=options or {"app_key": KEY, "token": TOKEN},
        client_factory=factory,
        virtual=virtual,
        token_cache=cache,
        venue=venue,
        clock=lambda: NOW,
    )
    return broker, calls


def reply(body, status=200):
    return status, body, {}


def test_unregistered_and_no_order_capability():
    broker, _ = make()
    assert "nh" not in BROKERS
    assert not broker.capabilities().supports_limit_order
    assert not broker.capabilities().supports_market_order


def test_token_form_live_host_cache_and_mock_read_host():
    cache = {}
    responses = {
        "/oauth2/token": reply({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400}),
        "/krstock/quote/v1/currentPrice": reply(
            {
                "Output_0": {
                    "stck_prpr": 15000,
                    "bidp": 14900,
                    "askp": 15000,
                    "stck_prdy_clpr": 14500,
                },
                "rsp_cd": "00166",
            }
        ),
    }
    broker, calls = make(responses, options={"app_key": KEY, "app_secret": SECRET}, virtual=True, cache=cache)
    quote = broker.get_quote("005930")
    assert quote.last == Decimal(15000) and quote.bid == Decimal(14900)
    host, path, headers, data = calls[0]
    assert host == NH_API_BASE_URL and path == "/oauth2/token"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(data.decode()) == {
        "appkey": [KEY],
        "appsecretkey": [SECRET],
        "grant_type": ["client_credentials"],
        "scope": ["oob"],
    }
    host, path, headers, data = calls[1]
    assert host == NH_MOCK_BASE_URL and path.endswith("currentPrice")
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["x-client-id"] == KEY
    assert "x-client-secret" not in headers and SECRET not in str(headers)
    assert json.loads(data) == {"Input_0": {"market_cd": "KRX", "iem_cd": "005930"}}
    assert cache[KEY] == broker.token_state()
    broker.get_quote("005930")
    assert sum(path == "/oauth2/token" for _, path, _, _ in calls) == 1


def test_stored_token_and_shared_cache_reuse():
    cache = {KEY: {"token": TOKEN, "expires_at": (NOW + timedelta(hours=1)).isoformat()}}
    responses = {"/krstock/quote/v1/currentPrice": reply({"Output_0": {"stck_prpr": 1}, "rsp_cd": "00000"})}
    broker, calls = make(responses, options={"app_key": KEY, "app_secret": SECRET}, cache=cache)
    assert broker.get_quote("005930").last == 1
    assert len(calls) == 1 and calls[0][2]["authorization"] == f"Bearer {TOKEN}"


def test_balance_positions_and_buying_power_exact_inputs():
    responses = {
        "/krstock/inquiry/v1/balance": reply(BALANCE),
        "/krstock/inquiry/v1/sellableQuantity": reply(
            {"Output_0": {"iem_cd": "005930", "sll_pbl_qty": 1}, "rsp_cd": "00000"}
        ),
        "/krstock/inquiry/v1/buyableQuantity": reply({"Output_0": {"orr_pbl_qty": 3}, "rsp_cd": "00221"}),
    }
    broker, calls = make(responses)
    assert broker.get_balance().cash_available == 40000
    assert broker.get_balance().position_value == 30000
    (position,) = broker.get_positions()
    assert position.quantity == 2 and position.sellable == 1
    assert broker.available_qty("005930") == 1
    assert broker.buyable_quantity("005930", price=Decimal(15000)) == 3
    balance_request = next(json.loads(data)["Input_0"] for _, path, _, data in calls if path.endswith("/balance"))
    assert balance_request == {
        "act_no": "12345678901",
        "bnc_bse_cd": "5",
        "ltg_aot_dit_cd": "9",
        "aet_bse": "1",
        "qut_dit_cd": "KRX",
        "aly_qut_cd": "2",
    }
    assert json.loads(calls[-1][3])["Input_0"] == {
        "ost_dit_cd": "1",
        "act_no": "12345678901",
        "iem_cd": "005930",
        "nmn_pr_tp_cd": "01",
        "orr_pr": 15000,
    }


def test_zero_cash_is_real_only_with_required_block_and_fields():
    broker, _ = make(
        {
            "/krstock/inquiry/v1/balance": reply(
                {
                    "Output_0": {"dca": 0, "orr_pbl_amt": "0", "tot_eal_amt": 0},
                    "rsp_cd": "13578",
                }
            )
        }
    )
    assert broker.get_balance().cash_available == 0
    broker, _ = make({"/krstock/inquiry/v1/balance": reply({"rsp_cd": "13578"})})
    with pytest.raises(InvalidResponseError):
        broker.get_balance()
    broker, _ = make({"/krstock/inquiry/v1/balance": reply({"Output_0": {}, "rsp_cd": "00000"})})
    with pytest.raises(InvalidResponseError):
        broker.get_balance()


def test_business_and_http_errors_never_expose_credentials():
    for response, error in [
        (reply({"rsp_cd": "99999", "rsp_msg": SECRET}), BrokerRejectedError),
        (reply({}, 429), TransportError),
        (reply({}, 500), TransportError),
    ]:
        broker, _ = make(
            {"/krstock/inquiry/v1/balance": response}, options={"app_key": KEY, "token": TOKEN, "app_secret": SECRET}
        )
        with pytest.raises(error) as caught:
            broker.get_balance()
        assert SECRET not in str(caught.value) and TOKEN not in str(caught.value)


def test_only_401_renews_and_replays_once():
    responses = {
        "/oauth2/token": reply({"access_token": "new-token", "expires_in": 86400}),
        "/krstock/inquiry/v1/balance": [reply({}, 401), reply(BALANCE)],
    }
    broker, calls = make(responses, options={"app_key": KEY, "token": TOKEN, "app_secret": SECRET})
    assert broker.get_balance().cash_available == 40000
    assert [path for _, path, _, _ in calls].count("/oauth2/token") == 1
    assert calls[-1][2]["authorization"] == "Bearer new-token"
    responses = {"/krstock/inquiry/v1/balance": reply({}, 429)}
    broker, calls = make(responses, options={"app_key": KEY, "token": TOKEN, "app_secret": SECRET})
    with pytest.raises(TransportError):
        broker.get_balance()
    assert len(calls) == 1


def test_mock_host_requires_explicit_boolean_and_us_is_locked():
    with pytest.raises(ConfigurationError):
        make(virtual="yes")
    with pytest.raises(Exception):
        NH(account="a", client_options={"app_key": KEY, "token": TOKEN}, market="US")


def test_account_type_must_match_host_and_account_list():
    with pytest.raises(ConfigurationError):
        make(options={"app_key": KEY, "token": TOKEN, "acct_type": "03"})
    broker, calls = make(
        {
            "/n2/acctinfo": reply(
                {
                    "Output_0": [{"acct_no": "12345678901", "acct_type": "03"}],
                    "rsp_cd": "00000",
                }
            )
        },
        virtual=True,
    )
    assert broker.verify_account() == "03"
    assert json.loads(calls[0][3]) == {"Input_0": {}}
    broker, _ = make(
        {
            "/n2/acctinfo": reply(
                {
                    "Output_0": [{"acct_no": "12345678901", "acct_type": "01"}],
                    "rsp_cd": "00000",
                }
            )
        },
        virtual=True,
    )
    with pytest.raises(ConfigurationError):
        broker.verify_account()


def test_rollback_sample_remove_module_leaves_factory_unchanged():
    # Rollback of this unregistered adapter is one-file removal; factory still has only prior brokers.
    assert tuple(sorted(BROKERS)) == ("dbfi", "kis", "toss")
