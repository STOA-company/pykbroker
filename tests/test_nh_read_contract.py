"""NH PLUG read contract from official common/krstock OpenAPI, with fake HTTP only."""

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pykbroker.brokers.nh import NH, NH_API_BASE_URL
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, InvalidResponseError, TransportError
from pykbroker.factory import BROKERS
from pykbroker.safety.rate_limit import RateLimiter

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
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response


def make(responses=None, *, options=None, virtual=False, cache=None, venue="KRX", rate_limiter=None):
    calls = []
    replies = responses or {}

    def factory(*, base_url):
        return FakeTransport(base_url=base_url, responses=replies, calls=calls)

    options = dict(options) if options is not None else {"app_key": KEY, "token": TOKEN}
    options.setdefault("acct_type", "03" if virtual is True else "01")
    broker = NH(
        account="12345678901",
        client_options=options,
        client_factory=factory,
        virtual=virtual,
        token_cache=cache,
        venue=venue,
        clock=lambda: NOW,
        rate_limiter=rate_limiter,
    )
    return broker, calls


def reply(body, status=200, headers=None):
    return status, body, headers or {}


def test_registered_and_limit_order_capability():
    broker, _ = make()
    assert BROKERS["nh"] is NH
    assert broker.capabilities().supports_limit_order
    assert broker.capabilities().supports_single_order_cancel
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
    assert host == NH_API_BASE_URL and path.endswith("currentPrice")
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
        "/krstock/inquiry/v1/buyableQuantity": reply(
            {"Output_0": {"orr_pbl_qty": 8, "csh_orr_pbl_qty": 3, "max_pbl_qty": 12}, "rsp_cd": "00221"}
        ),
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


def test_balance_reads_all_pages_before_using_final_totals():
    first = {
        "Output_0": {"dca": 0, "orr_pbl_amt": 0, "tot_eal_amt": 0},
        "Output_1": BALANCE["Output_1"],
        "rsp_cd": "00165",
    }
    last = {
        "Output_0": {"dca": 50000, "orr_pbl_amt": 40000, "tot_eal_amt": 60000},
        "Output_1": [{**BALANCE["Output_1"][0], "iem_cd": "000660"}],
        "rsp_cd": "00000",
    }
    responses = {
        "/krstock/inquiry/v1/balance": [
            reply(first, headers={"Cts": "next-page", "CTS_FLAG": "Y"}),
            reply(last, headers={"cts_flag": "N"}),
        ],
        "/krstock/inquiry/v1/sellableQuantity": [
            reply({"Output_0": {"iem_cd": "005930", "sll_pbl_qty": 1}, "rsp_cd": "00000"}),
            reply({"Output_0": {"iem_cd": "000660", "sll_pbl_qty": 1}, "rsp_cd": "00000"}),
        ],
    }
    broker, calls = make(responses)
    assert broker.get_balance().position_value == 60000
    assert json.loads(calls[1][3]) == json.loads(calls[0][3])
    assert calls[1][2]["cts"] == "next-page" and calls[1][2]["cts_flag"] == "Y"
    responses["/krstock/inquiry/v1/balance"] = [
        reply(first, headers={"cts": "next-page", "cts_flag": "Y"}),
        reply(last, headers={"cts_flag": "N"}),
    ]
    assert {p.symbol for p in broker.get_positions()} == {"005930", "000660"}


def test_balance_continuation_without_cursor_or_progress_fails_closed():
    first = {"Output_0": {"dca": 0, "orr_pbl_amt": 0, "tot_eal_amt": 0}, "rsp_cd": "00218"}
    for pages in (
        [reply(first)],
        [
            reply(first, headers={"cts": "same", "cts_flag": "Y"}),
            reply(first, headers={"cts": "same", "cts_flag": "Y"}),
        ],
    ):
        broker, _ = make({"/krstock/inquiry/v1/balance": pages})
        with pytest.raises(InvalidResponseError):
            broker.get_balance()


def test_balance_code_only_continuation_uses_response_cursor():
    first = {"Output_1": BALANCE["Output_1"], "rsp_cd": "00165"}
    last = {"Output_0": BALANCE["Output_0"], "rsp_cd": "00000"}
    broker, calls = make({"/krstock/inquiry/v1/balance": [reply(first, headers={"cts": "next"}), reply(last)]})
    assert broker.get_balance().position_value == 30000
    assert calls[1][2]["cts"] == "next"


def test_single_result_continuation_fails_closed():
    broker, _ = make(
        {
            "/krstock/quote/v1/currentPrice": reply(
                {"Output_0": {"iem_cd": "005930", "stck_prpr": 1000}, "rsp_cd": "00165"},
                headers={"cts": "next", "cts_flag": "Y"},
            )
        }
    )
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")


def test_balance_page_limit_fails_closed():
    current = [0.0]
    limiter = RateLimiter(
        {"read": 4}, clock=lambda: current[0], sleeper=lambda delay: current.__setitem__(0, current[0] + delay)
    )
    pages = [reply({"rsp_cd": "00165", "Output_1": []}, headers={"cts_flag": "Y", "cts": str(n)}) for n in range(101)]
    broker, calls = make({"/krstock/inquiry/v1/balance": pages}, rate_limiter=limiter)
    with pytest.raises(InvalidResponseError, match="page limit"):
        broker.get_balance()
    assert len(calls) == 100


def test_read_limiter_shares_credential_and_spaces_calls_at_four_per_second():
    current = [0.0]
    sleeps = []

    def advance(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    rows = [{**BALANCE["Output_1"][0], "iem_cd": f"{n:06d}"} for n in range(1, 7)]

    class TimedTransport(FakeTransport):
        def post(self, path, *, headers, data):
            times.append(current[0])
            return super().post(path, headers=headers, data=data)

    times = []
    calls = []

    def factory(*, base_url):
        return TimedTransport(base_url=base_url, responses=responses, calls=calls)

    responses = {
        "/krstock/inquiry/v1/balance": reply({"Output_0": BALANCE["Output_0"], "Output_1": rows, "rsp_cd": "00000"}),
        "/krstock/inquiry/v1/sellableQuantity": [
            reply({"Output_0": {"iem_cd": f"{n:06d}", "sll_pbl_qty": 1}, "rsp_cd": "00000"}) for n in range(1, 7)
        ],
    }
    limiter = RateLimiter({"read": 4}, clock=lambda: current[0], sleeper=advance)
    broker = NH(
        account="12345678901",
        client_options={"app_key": KEY, "token": TOKEN, "acct_type": "01"},
        client_factory=factory,
        clock=lambda: NOW,
        rate_limiter=limiter,
    )
    assert len(broker.get_positions()) == 6
    assert len(times) == 7 and all(b - a >= 0.25 for a, b in zip(times, times[1:]))
    assert len(sleeps) == 6
    other, _ = make({"/krstock/inquiry/v1/balance": reply(BALANCE)})
    same, _ = make({"/krstock/inquiry/v1/balance": reply(BALANCE)})
    assert other._limiter is same._limiter


def test_cash_buyable_quantity_uses_cash_field_and_requires_it():
    broker, calls = make(
        {
            "/krstock/inquiry/v1/buyableQuantity": reply(
                {"Output_0": {"orr_pbl_qty": 100, "csh_orr_pbl_qty": 2, "max_pbl_qty": 100}, "rsp_cd": "00000"}
            )
        }
    )
    assert broker.buyable_quantity("005930", price=Decimal(15000)) == 2
    assert len(calls) == 1
    broker, _ = make(
        {"/krstock/inquiry/v1/buyableQuantity": reply({"Output_0": {"orr_pbl_qty": 100}, "rsp_cd": "00000"})}
    )
    with pytest.raises(InvalidResponseError):
        broker.buyable_quantity("005930", price=Decimal(15000))


def test_cash_buyable_quantity_requires_explicit_limit_price():
    broker, calls = make({"/krstock/inquiry/v1/buyableQuantity": reply({"rsp_cd": "00000"})})
    with pytest.raises(ConfigurationError, match="limit price"):
        broker.buyable_quantity("005930")
    assert calls == []


def test_same_symbol_cash_and_credit_rows_share_one_cash_sellable_result():
    rows = [
        {**BALANCE["Output_1"][0], "itg_bnc_qty": 2, "phs_pr": 10000, "eal_amt": 30000},
        {**BALANCE["Output_1"][0], "itg_bnc_qty": 3, "phs_pr": 12000, "eal_amt": 45000, "itg_bnc_tp_cd": "credit"},
    ]
    responses = {
        "/krstock/inquiry/v1/balance": reply({"Output_0": BALANCE["Output_0"], "Output_1": rows, "rsp_cd": "00000"}),
        "/krstock/inquiry/v1/sellableQuantity": reply(
            {"Output_0": {"iem_cd": "005930", "sll_pbl_qty": 2}, "rsp_cd": "00000"}
        ),
    }
    broker, calls = make(responses)
    (position,) = broker.get_positions()
    assert position.quantity == 5 and position.sellable == 2
    assert position.average_price == 11200 and position.market_value == 75000
    assert [path for _, path, _, _ in calls].count("/krstock/inquiry/v1/sellableQuantity") == 1
    responses["/krstock/inquiry/v1/sellableQuantity"] = reply(
        {"Output_0": {"iem_cd": "005930", "sll_pbl_qty": 6}, "rsp_cd": "00000"}
    )
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_business_token_error_renews_once_and_replaces_stale_cache():
    error = reply({"rsp_cd": "IGW40043", "rsp_msg": "token expired"})
    cache = {KEY: {"token": TOKEN, "expires_at": (NOW + timedelta(hours=1)).isoformat()}}
    responses = {
        "/oauth2/token": reply({"access_token": "new-token", "expires_in": 86400}),
        "/krstock/inquiry/v1/balance": [error, reply(BALANCE)],
    }
    broker, calls = make(responses, options={"app_key": KEY, "token": TOKEN, "app_secret": SECRET}, cache=cache)
    assert broker.get_balance().cash_available == 40000
    assert cache[KEY]["token"] == "new-token"
    assert [path for _, path, _, _ in calls].count("/oauth2/token") == 1
    assert calls[-1][2]["authorization"] == "Bearer new-token"
    responses["/krstock/inquiry/v1/balance"] = [error, error]
    cache = {}
    broker, calls = make(responses, options={"app_key": KEY, "token": TOKEN, "app_secret": SECRET}, cache=cache)
    with pytest.raises(ConfigurationError):
        broker.get_balance()
    assert [path for _, path, _, _ in calls].count("/oauth2/token") == 1
    assert KEY not in cache


def test_rejected_token_without_secret_is_removed_from_cache():
    cache = {KEY: {"token": TOKEN, "expires_at": (NOW + timedelta(hours=1)).isoformat()}}
    broker, calls = make(
        {"/krstock/inquiry/v1/balance": reply({"rsp_cd": "IGW40043"})},
        options={"app_key": KEY, "token": TOKEN},
        cache=cache,
    )
    with pytest.raises(ConfigurationError):
        broker.get_balance()
    assert KEY not in cache and len(calls) == 1


def test_unknown_negated_completion_and_malformed_message_fail_closed():
    for body, error in [
        (
            {"Output_0": BALANCE["Output_0"], "rsp_cd": "99999", "rsp_msg": "조회가 완료되지 않았습니다."},
            BrokerRejectedError,
        ),
        (
            {"Output_0": BALANCE["Output_0"], "rsp_cd": "99999", "rsp_msg": "조회 미완료되었습니다."},
            BrokerRejectedError,
        ),
        (
            {"Output_0": BALANCE["Output_0"], "rsp_cd": "00000", "rsp_msg": "조회가 완료되지 않았습니다."},
            BrokerRejectedError,
        ),
        ({"Output_0": BALANCE["Output_0"], "rsp_cd": "99999", "message": "malformed"}, InvalidResponseError),
        ({"Output_0": BALANCE["Output_0"], "rsp_cd": "00000", "rsp_msg": 42}, InvalidResponseError),
    ]:
        broker, _ = make({"/krstock/inquiry/v1/balance": reply(body)})
        with pytest.raises(error):
            broker.get_balance()


def test_quote_rejects_different_response_symbol():
    broker, _ = make(
        {
            "/krstock/quote/v1/currentPrice": reply(
                {"Output_0": {"iem_cd": "000660", "stck_prpr": 15000}, "rsp_cd": "00000"}
            )
        }
    )
    with pytest.raises(InvalidResponseError):
        broker.get_quote("005930")


def test_cash_balance_uses_lower_deposit_when_order_amount_is_higher():
    body = {"Output_0": {"dca": 1000, "orr_pbl_amt": 5000, "tot_eal_amt": 0}, "rsp_cd": "00000"}
    broker, _ = make({"/krstock/inquiry/v1/balance": reply(body)})
    assert broker.get_balance().cash_available == 1000


def test_quote_requires_expected_output_block():
    broker, _ = make({"/krstock/quote/v1/currentPrice": reply({"rsp_cd": "00000"})})
    with pytest.raises(InvalidResponseError, match="missing Output_0"):
        broker.get_quote("005930")


def test_sellable_quantity_rejects_different_symbol():
    broker, _ = make(
        {
            "/krstock/inquiry/v1/sellableQuantity": reply(
                {"Output_0": {"iem_cd": "000660", "sll_pbl_qty": 2}, "rsp_cd": "00000"}
            )
        }
    )
    with pytest.raises(InvalidResponseError):
        broker.available_qty("005930")


def test_mock_host_requires_explicit_boolean_and_us_is_locked():
    with pytest.raises(ConfigurationError):
        make(virtual="yes")
    with pytest.raises(Exception):
        NH(account="a", client_options={"app_key": KEY, "token": TOKEN, "acct_type": "01"}, market="US")


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
    assert calls[0][0] == NH_API_BASE_URL
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


def test_older_constructor_token_does_not_replace_fresher_shared_cache():
    fresh = {"token": "shared-fresh", "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    cache = {KEY: fresh.copy()}
    broker, calls = make(
        {"/krstock/quote/v1/currentPrice": reply({"Output_0": {"stck_prpr": 1}, "rsp_cd": "00000"})},
        options={
            "app_key": KEY,
            "token": "expired-local",
            "expires_at": (NOW - timedelta(hours=1)).isoformat(),
            "app_secret": SECRET,
        },
        cache=cache,
    )
    assert cache[KEY] == fresh
    assert broker.get_quote("005930").last == 1
    assert len(calls) == 1 and calls[0][2]["authorization"] == "Bearer shared-fresh"


@pytest.mark.parametrize("first", [reply({}, 401), reply({"rsp_cd": "IGW40043"})])
def test_rejected_token_reuses_newer_shared_token_without_issuance(first):
    cache = {}

    def concurrent_refresh():
        cache[KEY] = {"token": "other-worker-token", "expires_at": (NOW + timedelta(hours=2)).isoformat()}
        return first

    broker, calls = make(
        {"/krstock/inquiry/v1/balance": [concurrent_refresh, reply(BALANCE)]},
        options={"app_key": KEY, "token": TOKEN},
        cache=cache,
    )
    assert broker.get_balance().cash_available == 40000
    assert [path for _, path, _, _ in calls] == ["/krstock/inquiry/v1/balance"] * 2
    assert calls[1][2]["authorization"] == "Bearer other-worker-token"
    assert cache[KEY]["token"] == "other-worker-token"


def test_known_no_data_response_means_empty_positions_and_zero_sellable_only():
    broker, calls = make(
        {
            "/krstock/inquiry/v1/balance": reply({"rsp_cd": "13578"}),
            "/krstock/inquiry/v1/sellableQuantity": reply({"rsp_cd": "13578"}),
        }
    )
    assert broker.get_positions() == []
    assert broker.available_qty("005930") == 0
    assert [path for _, path, _, _ in calls].count("/krstock/inquiry/v1/sellableQuantity") == 1
    with pytest.raises(InvalidResponseError):
        broker.get_balance()


def test_sellable_quantity_accepts_a_prefixed_response_symbol():
    broker, _ = make(
        {
            "/krstock/inquiry/v1/sellableQuantity": reply(
                {"Output_0": {"iem_cd": "A005930", "sll_pbl_qty": 2}, "rsp_cd": "00000"}
            )
        }
    )
    assert broker.available_qty("005930") == 2


@pytest.mark.parametrize("message", ["조회완료", "정상 조회 완료 (1건)"])
def test_known_success_accepts_completion_message_variants(message):
    broker, _ = make(
        {"/krstock/inquiry/v1/balance": reply({"Output_0": BALANCE["Output_0"], "rsp_cd": "00000", "rsp_msg": message})}
    )
    assert broker.get_balance().cash_available == 40000


@pytest.mark.parametrize(
    "message", ["불완료", "완료 (실패)", "처리 완료 (오류)", "오류 처리 완료", "조회 미완료되었습니다."]
)
@pytest.mark.parametrize("code", ["99999", "00000"])
def test_read_completion_never_accepts_failed_or_arbitrary_wording(message, code):
    broker, _ = make(
        {"/krstock/inquiry/v1/balance": reply({"Output_0": BALANCE["Output_0"], "rsp_cd": code, "rsp_msg": message})}
    )
    with pytest.raises(BrokerRejectedError):
        broker.get_balance()
