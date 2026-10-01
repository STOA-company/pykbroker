"""Offline tests for examples/kiwoom_mock_roundtrip.py.

The script is not part of the installed package, so it is loaded from its file path (matching how a
reviewer or CI would run `python examples/kiwoom_mock_roundtrip.py`). Every scenario here injects its own
fake transport via `client_factory`; nothing here opens a socket (the suite-wide `no_network` fixture in
tests/conftest.py would fail loudly if something tried to).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pykbroker.safety.rate_limit import RateLimiter

MODULE_PATH = Path(__file__).parent.parent / "examples" / "kiwoom_mock_roundtrip.py"
SPEC = importlib.util.spec_from_file_location("kiwoom_mock_roundtrip", MODULE_PATH)
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

FAST_RATES = {tr: 1000 for tr in ("ka10001", "ka10004", "kt10000", "kt10001", "kt10003", "ka10075", "kt00007")}


class FakeClient:
    """Keyed by the `api-id` header when present (TR calls), else by the request path (token calls)."""

    def __init__(self, base_url, responses):
        self.base_url = base_url
        self.calls: list[dict] = []
        self._responses = {key: list(queue) for key, queue in responses.items()}

    def post(self, path, *, headers, json):
        key = headers.get("api-id") or path
        self.calls.append({"path": path, "headers": dict(headers), "json": dict(json), "key": key})
        queue = self._responses.get(key)
        if not queue:
            raise AssertionError(f"no more fake responses queued for {key!r} (path={path!r})")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, tuple) else (response, {"cont-yn": "N"})


def make_factory(responses):
    """One shared FakeClient across every call the script makes (token issuance and TR calls alike)."""
    holder: dict[str, FakeClient] = {}

    def factory(*, base_url, timeout=30.0):
        if "client" not in holder:
            holder["client"] = FakeClient(base_url, responses)
        return holder["client"]

    return factory, holder


TOKEN_PATH = "/oauth2/token"
TOKEN_RESPONSE = {
    "expires_dt": "20991231235959",
    "token_type": "bearer",
    "token": "fixture-mock-token",
    "return_code": 0,
    "return_msg": "정상적으로 처리되었습니다",
}
# Synthetic (no live response captured): an "up" day so no price field carries a "-" prefix, tick=100
# for every value here (all in the [50000, 200000) TICK_BANDS row -- see rules/kr.py).
QUOTE_BODY = {
    "stk_cd": "005930",
    "stk_nm": "삼성전자",
    "cur_prc": "72000",
    "upl_pric": "93600",
    "lst_pric": "50400",
    "base_pric": "72000",
    "return_code": 0,
    "return_msg": "정상적으로 처리되었습니다",
}
BOOK_BODY = {
    "bid_req_base_tm": "093000",
    "buy_fpr_bid": "71900",
    "sel_fpr_bid": "72100",
    "return_code": 0,
    "return_msg": "조회가 완료되었습니다",
}
PLACE_RESPONSE = {"ord_no": "00024", "return_code": 0, "return_msg": "매수주문이 완료되었습니다"}
OPEN_ORDER_ROW = {
    "acnt_no": "0000000000",
    "ord_no": "00024",
    "stk_cd": "005930",
    "stk_nm": "삼성전자",
    "ord_stt": "접수",
    "ord_qty": "1",
    "ord_pric": "+50400",
    "oso_qty": "1",
    "cntr_tot_amt": "0",
    "orig_ord_no": "0000000",
    "io_tp_nm": "+매수",
    "trde_tp": "보통",
    "tm": "093005",
    "cntr_no": "",
    "cntr_pric": "",
    "cntr_qty": "0",
    "cur_prc": "+72000",
    "sel_bid": "0",
    "buy_bid": "0",
    "unit_cntr_pric": "",
    "unit_cntr_qty": "",
    "tdy_trde_cmsn": "0",
    "tdy_trde_tax": "0",
    "stex_tp": "1",
    "stex_tp_txt": "KRX",
    "sor_yn": "N",
    "stop_pric": "",
}
OPEN_ORDERS_BODY = {"oso": [OPEN_ORDER_ROW], "return_code": 0, "return_msg": "조회가 완료되었습니다"}
EMPTY_OPEN_ORDERS_BODY = {"oso": [], "return_code": 0, "return_msg": "조회가 완료되었습니다"}
CANCEL_RESPONSE = {
    "ord_no": "00025",
    "base_orig_ord_no": "00024",
    "cncl_qty": "000000000001",
    "return_code": 0,
    "return_msg": "매수취소 주문입력이 완료되었습니다",
}


def full_responses():
    return {
        TOKEN_PATH: [dict(TOKEN_RESPONSE)],
        "ka10001": [dict(QUOTE_BODY), dict(QUOTE_BODY)],  # get_quote's read, then get_rules's read
        "ka10004": [dict(BOOK_BODY)],
        "kt10000": [dict(PLACE_RESPONSE)],
        "ka10075": [dict(OPEN_ORDERS_BODY), dict(EMPTY_OPEN_ORDERS_BODY)],
        "kt10003": [dict(CANCEL_RESPONSE)],
    }


FAKE_ENV = {
    script.APP_KEY_ENV: "FAKE-APP-KEY-MARKER",
    script.SECRET_KEY_ENV: "FAKE-SECRET-KEY-MARKER",
    script.ACCOUNT_ENV: "fixture-account-0000",
}


# --- normal round trip ---


def test_normal_roundtrip_places_and_cancels_with_a_fake_transport():
    factory, holder = make_factory(full_responses())
    result = script.run(execute=True, env=FAKE_ENV, client_factory=factory, rate_limiter=RateLimiter(FAST_RATES))

    assert result["status"] == "ok", result
    assert result["order_id_last4"] == "0024"
    assert all(step["ok"] for step in result["steps"]), result["steps"]
    names = [step["name"] for step in result["steps"]]
    assert names == [
        "host_check",
        "issue_token",
        "read_quote",
        "read_rules",
        "place_order",
        "confirm_open",
        "cancel",
        "confirm_cancel",
    ]

    client = holder["client"]
    assert [call["key"] for call in client.calls] == [
        TOKEN_PATH,
        "ka10001",
        "ka10004",
        "ka10001",
        "kt10000",
        "ka10075",
        "kt10003",
        "ka10075",
    ]
    # symbol and quantity are exactly the fixed constants, on both the order and the cancel
    assert client.calls[4]["json"] == {
        "dmst_stex_tp": "KRX",
        "stk_cd": "005930",
        "ord_qty": "1",
        "ord_uv": "50400",
        "trde_tp": "0",
    }
    assert client.calls[6]["json"] == {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": "00024",
        "stk_cd": "005930",
        "cncl_qty": "1",
    }


def test_dry_run_is_the_default_and_touches_no_transport():
    def exploding_factory(*, base_url, timeout=30.0):
        raise AssertionError("dry-run must never build a network client")

    result = script.run(execute=False, client_factory=exploding_factory)
    assert result["status"] == "dry_run"
    assert "would_do" in result and "order_id_last4" not in result

    # main() with no argv defaults to the same dry-run path
    code = script.main([], env={}, client_factory=exploding_factory)
    assert code == 0


@pytest.mark.parametrize(
    "env",
    [
        {},
        {script.APP_KEY_ENV: "fake"},
        {script.SECRET_KEY_ENV: "fake"},
        {script.APP_KEY_ENV: "  ", script.SECRET_KEY_ENV: "fake"},
        {script.APP_KEY_ENV: "fake", script.SECRET_KEY_ENV: "  "},
    ],
)
def test_missing_keys_exit_safely_with_zero_calls(env, capsys):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        pytest.fail("missing keys must stop before client construction")

    code = script.main(["--execute"], env=env, client_factory=factory)
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "missing_keys"
    assert "키 없음" in result["reason"]
    assert [step["name"] for step in result["steps"]] == ["host_check"]
    assert calls == []


# --- guard: real-server host is rejected ---


def test_real_server_host_constant_drift_aborts_before_any_credential_or_network_use(monkeypatch):
    monkeypatch.setattr(script, "KIWOOM_MOCK_BASE_URL", "https://api.kiwoom.com")

    def exploding_factory(*, base_url, timeout=30.0):
        raise AssertionError("host guard must fire before a client is ever built")

    result = script.run(execute=True, env={}, client_factory=exploding_factory)
    assert result["status"] == "manual_check_needed"
    assert result["steps"] == [{"name": "host_check", "ok": False}]


def test_guarded_client_factory_rejects_the_real_host_and_anything_unexpected():
    with pytest.raises(script.RoundTripAbort):
        script._guarded_client_factory(base_url="https://api.kiwoom.com")
    with pytest.raises(script.RoundTripAbort):
        script._guarded_client_factory(base_url="https://evil.example.com")


# --- guard: unknown/1999 response aborts without retry ---


def test_an_unclear_write_response_aborts_once_with_no_retry():
    responses = full_responses()
    responses["kt10000"] = [{**PLACE_RESPONSE, "return_code": 1999}]  # exactly one queued: a retry would crash the fake
    factory, holder = make_factory(responses)

    result = script.run(execute=True, env=FAKE_ENV, client_factory=factory, rate_limiter=RateLimiter(FAST_RATES))

    assert result["status"] == "manual_check_needed"
    assert "수동 확인" in result["reason"]
    assert sum(1 for call in holder["client"].calls if call["key"] == "kt10000") == 1


def test_an_explicit_rejection_is_reported_distinctly_from_an_unclear_answer():
    responses = full_responses()
    responses["kt10000"] = [{**PLACE_RESPONSE, "return_code": 1, "return_msg": "잔고 부족"}]
    factory, _ = make_factory(responses)

    result = script.run(execute=True, env=FAKE_ENV, client_factory=factory, rate_limiter=RateLimiter(FAST_RATES))

    assert result["status"] == "rejected"
    assert "잔고 부족" not in json.dumps(result, ensure_ascii=False)  # vendor detail is not echoed


# --- guard: a signed price is refused, not guessed ---


def test_a_signed_current_price_aborts_as_manual_check_not_a_guess():
    responses = full_responses()
    responses["ka10001"] = [{**QUOTE_BODY, "cur_prc": "-72000"}]
    factory, holder = make_factory(responses)

    result = script.run(execute=True, env=FAKE_ENV, client_factory=factory, rate_limiter=RateLimiter(FAST_RATES))

    assert result["status"] == "manual_check_needed"
    assert result["steps"][-1] == {
        "name": "read_quote",
        "ok": False,
        "error": "SpecUnverifiedError",
        "minus_sign_seen": True,
    }
    assert [call["key"] for call in holder["client"].calls] == [TOKEN_PATH, "ka10001"]


def test_plus_signed_prices_complete_only_the_fake_roundtrip():
    responses = full_responses()
    for body in responses["ka10001"]:
        for field in ("cur_prc", "upl_pric", "lst_pric", "base_pric"):
            body[field] = "+" + body[field]
    for field in ("buy_fpr_bid", "sel_fpr_bid"):
        responses["ka10004"][0][field] = "+" + responses["ka10004"][0][field]
    factory, holder = make_factory(responses)
    result = script.run(execute=True, env=FAKE_ENV, client_factory=factory, rate_limiter=RateLimiter(FAST_RATES))
    assert result["status"] == "ok"
    assert len(holder["client"].calls) == 8
    assert holder["client"].calls[4]["json"]["ord_uv"] == "50400"


# --- guard: credentials are never printed ---


def test_credentials_never_appear_in_the_json_result_or_on_stdout(capsys):
    factory, _ = make_factory(full_responses())
    code = script.main(["--execute"], env=FAKE_ENV, client_factory=factory)
    out = capsys.readouterr().out
    assert code == 0
    for secret in (FAKE_ENV[script.APP_KEY_ENV], FAKE_ENV[script.SECRET_KEY_ENV], TOKEN_RESPONSE["token"]):
        assert secret not in out
    # the account label is shown, but masked
    assert FAKE_ENV[script.ACCOUNT_ENV] not in out
    assert json.loads(out)["account"] == "***0000"


# --- guard: a redirect is never followed ---


def test_no_redirect_handler_refuses_to_build_a_redirected_request():
    handler = script._NoRedirectHandler()
    built = handler.redirect_request(
        req=object(), fp=None, code=302, msg="Found", headers={}, newurl="https://attacker.example/steal"
    )
    assert built is None
