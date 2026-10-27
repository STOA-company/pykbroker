"""증권사 한 판(F2 D6264) — 어댑터가 T 관문에 내는 칸: KB·LS 국내 상·하한가, LS 국내 최우선 호가,
키움 부분체결 뒤 남은 수량 취소 확인, NH 접수번호(mkt)로 체결 확인. 오프라인 전송만.

공식 근거(각 어댑터 주석에 URL):
  KB IVU10140 ulmt_prc·llmt_prc — openapi.kbsec.com/api/kbs/guide/excel/b2c (2026-10-28 다시 받음, 예제 IVU10140.json)
  LS t1101 offerho1·bidho1·jnilclose · t1102 uplmtprice·dnlmtprice 
    — openapi.ls-sec.co.kr tr-guides.json 20261026021444748
  키움 kt00007 ori_ord·mdfy_cncl·ord_qty·cnfm_qty — Kiwoom-REST-API kiwoom_api_spec.json @953e5db
  NH dailyOrderExecution(itg_orr_no 만)·cashBuy(mkt_orr_no 만) — nhplug.com/openapi-docs/krstock/openapi.json
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.exceptions import InvalidResponseError
from pykbroker.models import Order
from tests.test_kiwoom_order_contract import LEDGER, OFFICIAL, held
from tests.test_kiwoom_order_contract import make as kiwoom
from tests.test_ls_contract import make as ls
from tests.test_ls_contract import page as ls_page
from tests.test_nh_order_contract import DAILY, reply
from tests.test_nh_order_contract import make as nh

# --- KB 국내 상·하한가 (②) ---


def _kb(body, *, halt=None):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL

    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"}, "dataBody": body}
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
                client=client, halt_provider=halt)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    return broker, client


# Official IVU10140.json example values (now_prc 368500, ulmt_prc 479000, llmt_prc 258000).
KB_ROW = {"now_prc": "368500", "ulmt_prc": "479000", "llmt_prc": "258000", "bdy_cls_prc": "368500",
          "s_sq1_askprc": "368600", "b_sq1_askprc": "368500"}


def test_kb_kr_rules_carry_the_venue_band():
    broker, client = _kb(KB_ROW, halt=lambda _: False)
    rules = broker.get_rules("005930")
    assert (rules.limit_up, rules.limit_down, rules.limits_source) == (Decimal(479000), Decimal(258000), "venue")
    assert rules.halted is False
    path = client.post.call_args.args[0]
    assert path == "/api/v1/ivu10140"
    assert client.post.call_args.kwargs["body"]["dataBody"] == {"excg_clsf": "1", "shrt_cd": "005930"}


@pytest.mark.parametrize("change", [{"ulmt_prc": "0"}, {"llmt_prc": ""}, {"ulmt_prc": None}])
def test_kb_kr_half_or_zero_band_stays_unknown(change):
    rules = _kb({**KB_ROW, **change})[0].get_rules("005930")
    assert (rules.limit_up, rules.limit_down, rules.limits_source) == (None, None, None)


def test_kb_kr_band_that_excludes_the_last_price_is_invalid():
    with pytest.raises(InvalidResponseError):
        _kb({**KB_ROW, "ulmt_prc": "300000"})[0].get_rules("005930")


# --- LS 국내 호가 (③) · 상·하한가 ---


def test_ls_kr_quote_reads_the_best_bid_and_ask():
    quote = ls({"t1101": [ls_page("t1101")]})[0].get_quote("005930")
    assert (quote.last, quote.bid, quote.ask, quote.prev_close) == (
        Decimal(70000), Decimal(70000), Decimal(70100), Decimal(69500))


@pytest.mark.parametrize("book,expected", [
    ({"bidho1": "0", "offerho1": "70100"}, (None, Decimal(70100))),  # a halted/locked side is empty
    ({"bidho1": "70200", "offerho1": "70100"}, (None, None)),  # crossed
])
def test_ls_kr_quote_empty_or_crossed_side_is_not_a_spread(book, expected):
    body = ls_page("t1101")
    body["t1101OutBlock"] = {**body["t1101OutBlock"], **book}
    quote = ls({"t1101": [body]})[0].get_quote("005930")
    assert (quote.bid, quote.ask) == expected


def test_ls_kr_quote_for_another_symbol_is_invalid():
    body = ls_page("t1101")
    body["t1101OutBlock"] = {**body["t1101OutBlock"], "shcode": "000660"}
    with pytest.raises(InvalidResponseError):
        ls({"t1101": [body]})[0].get_quote("005930")


def test_ls_kr_rules_carry_the_venue_band():
    body = ls_page("t1102", t1102OutBlock={"price": "70000", "uplmtprice": "91000", "dnlmtprice": "49000"})
    broker, client, _ = ls({"t1102": [body]}, halt_provider=lambda _: False)
    rules = broker.get_rules("005930")
    assert (rules.limit_up, rules.limit_down, rules.limits_source, rules.halted) == (
        Decimal(91000), Decimal(49000), "venue", False)
    assert client.calls[0][2] == {"t1102InBlock": {"shcode": "005930", "exchgubun": "fixture-exchange"}}


def test_ls_kr_rules_without_a_band_stay_unknown():
    rules = ls({"t1102": [ls_page("t1102")]})[0].get_rules("005930")
    assert (rules.limit_up, rules.limit_down) == (None, None)


# --- 키움 부분체결 뒤 남은 수량 취소 확인 (⑤) ---


def _kiwoom_page(cancel_changes=None, **original):
    base = OFFICIAL["kt00007"][LEDGER][0]
    row = {**base, "trde_tp": "지정가", "ord_qty": "0000000003", "cntr_qty": "0000000001", "ord_remnq": "0000000000",
           **original}
    cancel = {**base, "ord_no": "0000051", "ori_ord": "0000050", "mdfy_cncl": "취소", "ord_qty": "0000000002",
              "cntr_qty": "0000000000", "ord_remnq": "0000000000", "cnfm_qty": "0000000002", **(cancel_changes or {})}
    return {**OFFICIAL["kt00007"], LEDGER: [row, cancel]}


def test_kiwoom_partial_fill_then_rest_cancelled_is_cancelled():
    result = kiwoom({"kt00007": [_kiwoom_page()]})[0].status(held())
    assert (result.status, result.filled, result.remaining, result.quantity) == ("cancelled", 1, 0, 3)


def test_kiwoom_whole_order_cancelled_before_any_fill_is_cancelled():
    page = _kiwoom_page({"ord_qty": "0000000003", "cnfm_qty": "0000000003"}, cntr_qty="0000000000")
    assert kiwoom({"kt00007": [page]})[0].status(held()).status == "cancelled"


@pytest.mark.parametrize("cancel_changes,original", [
    ({"mdfy_cncl": "정정"}, {}),  # an amend moves the rest to a new order: not cancelled
    ({"mdfy_cncl": "취소거부"}, {}),
    ({"cnfm_qty": "0000000000"}, {}),  # the venue has not confirmed the cancel yet
    ({"cnfm_qty": "0000000001"}, {}),
    ({"ord_qty": "0000000001", "cnfm_qty": "0000000001"}, {}),  # cancelled 1 of a rest of 2
    ({}, {"ord_remnq": "0000000002"}),  # the venue still shows a rest
])
def test_kiwoom_cancel_is_only_claimed_when_the_numbers_close(cancel_changes, original):
    page = _kiwoom_page(cancel_changes, **original)
    assert kiwoom({"kt00007": [page]})[0].status(held()).status == "unknown"


def test_kiwoom_cancel_without_a_confirmed_quantity_field_uses_the_cancel_rows_quantity():
    page = _kiwoom_page()
    del page[LEDGER][1]["cnfm_qty"]
    assert kiwoom({"kt00007": [page]})[0].status(held()).status == "cancelled"


# --- NH 접수번호(mkt)로 체결 확인 (⑤) ---


NH_ROW = {"itg_orr_no": 456, "iem_cd": "005930", "sby_dit_cd_nm": "매수", "nmn_pr_tp_cd_nm": "보통가",
          "orr_qty": 3, "orr_pr": 70000, "tot_cns_qty": 1, "ny_cns_qty": 2, "rmt_mkt_cd": "KRX",
          "org_itg_orr_no": "0", "cns_avg_uit_pr": "70000"}


def _nh_daily(*rows):
    page = reply([], message="조회완료", code="00000")
    page[1]["Output_0"] = list(rows)
    return page


def placed(**changes):
    return Order(**{"id": "mkt:123", "symbol": "005930", "side": "buy", "quantity": 3, "price": "70000",
                    "status": "accepted", **changes})


def test_nh_market_number_order_is_found_by_its_own_terms():
    broker, calls = nh({DAILY: [_nh_daily(NH_ROW)]})
    result = broker.status(placed())
    assert (result.id, result.status, result.filled, result.remaining) == ("mkt:123", "partial", 1, 2)
    assert len(calls) == 1


def test_nh_market_number_order_fully_filled_is_filled():
    row = {**NH_ROW, "tot_cns_qty": 3, "ny_cns_qty": 0}
    result = nh({DAILY: [_nh_daily(row)]})[0].status(placed())
    assert (result.status, result.filled, result.fill_price) == ("filled", 3, Decimal(70000))


@pytest.mark.parametrize("rows", [
    [NH_ROW, {**NH_ROW, "itg_orr_no": 457}],  # two identical orders: which one is unknown
    [{**NH_ROW, "orr_qty": 2, "ny_cns_qty": 1}],  # another quantity
    [{**NH_ROW, "orr_pr": 70100}],  # another price
    [{**NH_ROW, "sby_dit_cd_nm": "매도"}],  # another side
    [{**NH_ROW, "rmt_mkt_cd": "NXT"}],  # another venue
    [{**NH_ROW, "org_itg_orr_no": 455}],  # a correction/cancel row is not the order
    [],
])
def test_nh_market_number_order_without_exactly_one_original_row_is_unknown(rows):
    assert nh({DAILY: [_nh_daily(*rows)]})[0].status(placed()).status == "unknown"


def test_nh_market_number_order_pointed_at_by_a_cancel_row_is_unknown():
    cancel_row = {**NH_ROW, "itg_orr_no": 458, "org_itg_orr_no": 456, "orr_qty": 2, "tot_cns_qty": 0, "ny_cns_qty": 0}
    assert nh({DAILY: [_nh_daily(NH_ROW, cancel_row)]})[0].status(placed()).status == "unknown"


@pytest.mark.parametrize("missing", ["side", "quantity", "price"])
def test_nh_market_number_order_without_its_terms_is_unknown_without_a_read(missing):
    broker, calls = nh({DAILY: [_nh_daily(NH_ROW)]})
    assert broker.status(placed(**{missing: None})).status == "unknown"
    assert calls == []


# --- 키 검증 분류 칸 (T /stock_trade/account-verify) ---


def test_kiwoom_refusal_carries_the_integer_vendor_code_only():
    from pykbroker.brokers.kiwoom import _unpack
    from pykbroker.exceptions import BrokerRejectedError

    with pytest.raises(BrokerRejectedError) as caught:
        _unpack(({"return_code": 8001, "return_msg": "fixture"}, {}))
    assert caught.value.return_code == 8001 and "fixture" not in str(caught.value)
    with pytest.raises(BrokerRejectedError) as caught:
        _unpack(({"return_code": "8001"}, {}))
    assert caught.value.return_code is None


@pytest.mark.parametrize("status", [400, 401])
def test_nh_token_refusal_carries_the_http_status(status):
    from pykbroker.exceptions import ConfigurationError
    from tests.test_nh_review_fixes import KEY, SECRET
    from tests.test_nh_review_fixes import make as nh_auth

    broker, _ = nh_auth({"/oauth2/token": (status, {}, {})}, options={"app_key": KEY, "app_secret": SECRET,
                                                                      "acct_type": "01"})
    with pytest.raises(ConfigurationError) as caught:
        broker.issue_token()
    assert caught.value.status_code == status


def test_ls_every_quote_and_rules_tr_has_a_rate_in_the_real_limiter():
    """A Mock limiter hides a missing TR rate; the real one refuses it (caught by trader #651 walk)."""
    from pykbroker.brokers.ls import LS_RATES
    from pykbroker.safety.rate_limit import RateLimiter

    limiter = RateLimiter(LS_RATES)
    for tr in ("t1101", "t1102", "g3101", "g3104"):
        limiter.wait(tr)
    body = ls_page("t1102", t1102OutBlock={"price": "70000", "uplmtprice": "91000", "dnlmtprice": "49000"})
    from pykbroker.brokers.ls import LS
    from tests.test_ls_contract import CODES, FakeClient

    client = FakeClient({"t1101": [ls_page("t1101")], "t1102": [body]})
    broker = LS(account="fixture-account", client=client, codes=CODES, rate_limiter=RateLimiter(LS_RATES))
    assert broker.get_quote("005930").bid == Decimal(70000)
    assert broker.get_rules("005930").limit_up == Decimal(91000)
