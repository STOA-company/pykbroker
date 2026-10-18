"""Holdings continuation for pre-trade sell checks (trader #527 review P1-1/P1-2).

A held symbol on a later balance page must be sellable exactly; a list that does not end
within the page bound is refused (the caller treats it as «holdings unknown»), never cut short.
Synthetic responses shaped by the official KIS/LS/KB contracts; no vendor account recording.
"""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers import kis as kis_module
from pykbroker.brokers import ls as ls_module
from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import InvalidResponseError, SpecUnverifiedError
from pykbroker.safety.rate_limit import RateLimiter
from tests.test_ls_contract import CODES, page, us_balance

# ------------------------------------------------------------------ KIS US (CTRP6504R, tr_cont F/M → N)


def _kis_row(symbol, held, sellable):
    return {"pdno": symbol, "prdt_name": "Fixture", "ccld_qty_smtl1": held, "ord_psbl_qty1": sellable,
            "avg_unpr3": "100", "ovrs_now_pric1": "120.50", "frcr_evlu_amt2": "241.00"}


def _kis(first, nexts):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_balances.return_value = first
    responses = [
        SimpleNamespace(status_code=200, headers={"tr_cont": flag}, json=lambda body=body: deepcopy(body))
        for body, flag in nexts
    ]
    scope.client.request.side_effect = responses
    scope.account.build_body.side_effect = lambda body: {"CANO": "fixture", **body}
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api,
                 cash_provider=lambda scope: Decimal("1000"))
    return broker, scope


def test_kis_us_symbol_on_second_page_is_sellable():
    first = {"json_data": {"rt_cd": "0", "output1": [_kis_row("MSFT", "1", "1")]}, "tr_cont": "F"}
    broker, scope = _kis(first, [({"rt_cd": "0", "output1": [_kis_row("AAPL", "3", "2")]}, "D")])
    positions = {p.symbol: p for p in broker.get_positions()}
    assert set(positions) == {"MSFT", "AAPL"}
    assert positions["AAPL"].sellable == 2 and positions["AAPL"].quantity == 3
    (call,) = scope.client.request.call_args_list
    assert call.args[:2] == ("get", "/uapi/overseas-stock/v1/trading/inquire-present-balance")
    assert call.kwargs["headers"] == {"tr_id": "CTRP6504R", "tr_cont": "N"}
    assert call.kwargs["params"] == {"CANO": "fixture", "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                                     "TR_MKET_CD": "00", "INQR_DVSN_CD": "01"}


@pytest.mark.parametrize("flag", [None, "", "D", "E"])
def test_kis_us_last_page_flags_end_the_list(flag):
    first = {"json_data": {"rt_cd": "0", "output1": [_kis_row("AAPL", "2", "2")]}, "tr_cont": flag}
    broker, scope = _kis(first, [])
    assert [p.symbol for p in broker.get_positions()] == ["AAPL"]
    scope.client.request.assert_not_called()


@pytest.mark.parametrize("flag", ["X", 7])
def test_kis_us_unreadable_continuation_flag_is_refused(flag):
    first = {"json_data": {"rt_cd": "0", "output1": []}, "tr_cont": flag}
    broker, _ = _kis(first, [])
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_kis_us_page_bound_is_unknown_not_truncated():
    more = {"rt_cd": "0", "output1": [_kis_row("MSFT", "1", "1")]}
    first = {"json_data": deepcopy(more), "tr_cont": "M"}
    broker, scope = _kis(first, [(more, "M")] * kis_module.KIS_MAX_PAGES)
    with pytest.raises(InvalidResponseError, match="page limit"):
        broker.get_positions()
    assert scope.client.request.call_count == kis_module.KIS_MAX_PAGES - 1


def test_kis_us_failed_continuation_request_is_refused():
    first = {"json_data": {"rt_cd": "0", "output1": []}, "tr_cont": "F"}
    broker, scope = _kis(first, [])
    scope.client.request.side_effect = [SimpleNamespace(status_code=500, headers={}, json=dict)]
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


# ------------------------------------------------------------------ LS (t0424 cts_expcode, COSOQ00201 headers)


class PagedClient:
    """LS client that remembers each response's headers like the real transport."""

    def __init__(self, pages):
        self.base_url = ls_module.LS_API_BASE_URL
        self.pages = list(pages)
        self.calls = []
        self.response_headers = {}

    def post(self, path, *, headers, json):
        self.calls.append((path, deepcopy(headers), deepcopy(json)))
        body, response_headers = self.pages.pop(0)
        self.response_headers = None if response_headers is None else dict(response_headers)
        return deepcopy(body)


def _ls(pages, **kwargs):
    client = PagedClient(pages)
    broker = ls_module.LS(account="fixture-account", client=client, codes=CODES,
                          rate_limiter=Mock(spec=RateLimiter), **kwargs)
    return broker, client


def _t0424(rows, cursor):
    body = page("t0424")
    body["t0424OutBlock"] = {"cts_expcode": cursor}
    template = body["t0424OutBlock1"][0]
    body["t0424OutBlock1"] = [{**template, "expcode": code, "janqty": held, "mdposqt": sellable}
                              for code, held, sellable in rows]
    return body


def test_ls_kr_symbol_on_second_page_is_sellable():
    broker, client = _ls([
        (_t0424([("000660", "1", "1")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"}),
        (_t0424([("005930", "3", "2")], ""), {"tr_cont": "N", "tr_cont_key": ""}),
    ])
    positions = {p.symbol: p for p in broker.get_positions()}
    assert positions["005930"].sellable == 2 and positions["000660"].sellable == 1
    first, second = client.calls
    assert first[1]["tr_cont"] == "N" and first[2]["t0424InBlock"]["cts_expcode"] == ""
    assert second[1]["tr_cont"] == "Y" and second[1]["tr_cont_key"] == "k1"
    assert second[2]["t0424InBlock"]["cts_expcode"] == "005930"


def test_ls_kr_repeated_cursor_is_refused():
    broker, _ = _ls([(_t0424([("000660", "1", "1")], "005930"), {})] * 2)
    with pytest.raises(InvalidResponseError, match="repeated"):
        broker.get_positions()


def test_ls_kr_header_more_without_cursor_is_refused():
    broker, _ = _ls([(_t0424([("000660", "1", "1")], ""), {"tr_cont": "Y", "tr_cont_key": "k"})])
    with pytest.raises(InvalidResponseError, match="conflict"):
        broker.get_positions()


def test_ls_kr_page_bound_is_unknown_not_truncated():
    pages = [(_t0424([("000660", "1", "1")], f"{index:06d}"), {}) for index in range(ls_module.LS_MAX_PAGES)]
    broker, client = _ls(pages)
    with pytest.raises(InvalidResponseError, match="page limit"):
        broker.get_positions()
    assert len(client.calls) == ls_module.LS_MAX_PAGES


def _cosoq(symbol, held, sellable):
    body = us_balance()
    row = body["COSOQ00201OutBlock4"][0]
    body["COSOQ00201OutBlock4"] = [{**row, "ShtnIsuNo": symbol, "AstkBalQty": held, "AstkSellAbleQty": sellable}]
    return body


def test_ls_us_symbol_on_second_page_is_sellable():
    broker, client = _ls([
        (_cosoq("MSFT", 1, 1), {"tr_cont": "Y", "tr_cont_key": "next-1"}),
        (_cosoq("AAPL", 3, 2), {"tr_cont": "N", "tr_cont_key": ""}),
    ], market="US", venue="NASDAQ")
    positions = {p.symbol: p for p in broker.get_positions()}
    assert positions["AAPL"].sellable == 2 and positions["MSFT"].sellable == 1
    first, second = client.calls
    assert (first[1]["tr_cont"], first[1]["tr_cont_key"]) == ("N", "")
    assert (second[1]["tr_cont"], second[1]["tr_cont_key"]) == ("Y", "next-1")
    assert first[2] == second[2]


@pytest.mark.parametrize("headers", [{"tr_cont": "Y"}, {"tr_cont": "Y", "tr_cont_key": " "},
                                     {"tr_cont": "N", "tr_cont_key": "next"}, {"tr_cont": "?"}])
def test_ls_us_unreadable_continuation_is_refused(headers):
    broker, _ = _ls([(_cosoq("MSFT", 1, 1), headers)], market="US", venue="NASDAQ")
    with pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_ls_us_missing_headers_stay_unverified():
    broker, _ = _ls([(_cosoq("MSFT", 1, 1), None)], market="US", venue="NASDAQ")
    with pytest.raises(SpecUnverifiedError):
        broker.get_positions()


def test_ls_us_repeated_key_and_page_bound_are_refused():
    broker, _ = _ls([(_cosoq("MSFT", 1, 1), {"tr_cont": "Y", "tr_cont_key": "same"})] * 2,
                    market="US", venue="NASDAQ")
    with pytest.raises(InvalidResponseError):
        broker.get_positions()
    pages = [(_cosoq("MSFT", 1, 1), {"tr_cont": "Y", "tr_cont_key": f"k{index}"})
             for index in range(ls_module.LS_MAX_PAGES)]
    broker, client = _ls(pages, market="US", venue="NASDAQ")
    with pytest.raises(InvalidResponseError, match="page limit"):
        broker.get_positions()
    assert len(client.calls) == ls_module.LS_MAX_PAGES


# ------------------------------------------------------------------ KB KR (SSQM2952 → SSQM1801 nxt_key)


def _kb_envelope(body):
    return {"dataHeader": {"resultCode": "200", "processFlag": "A"}, "dataBody": body}


def _ssqm2952(rows, total):
    return _kb_envelope({"tl_data_cnt": str(total), "grid_cnt1": str(len(rows)), "Record1": [
        {"crncy_cd": "KRW", "is_cd": "A" + code, "is_nm": "fixture", "hld_q": held, "ordr_psbl_q": sellable,
         "byng_avr_prc": "100", "now_prc": "110", "val_amt": "330"} for code, held, sellable in rows]})


def _ssqm1801(rows, nxt):
    return _kb_envelope({"nxt_key": nxt, "grid_cnt1": str(len(rows)), "Record1": [
        {"is_no": "A" + code, "is_nm": "fixture", "gnrl_q": sellable, "ordr_psbl_q": sellable}
        for code, sellable in rows]})


def _kb(responses):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.side_effect = responses
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token", client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    return broker, client


def test_kb_kr_complete_snapshot_gives_exact_sellable():
    broker, client = _kb([_ssqm2952([("005930", "3", "2")], 1)])
    assert broker.sellable_quantity("005930") == Decimal(2)
    assert client.post.call_count == 1


def test_kb_kr_truncated_snapshot_reads_paged_sellable_listing():
    broker, client = _kb([
        _ssqm2952([("000660", "1", "1")], 21),
        _ssqm1801([("000660", "1")], "NEXT-1"),
        _ssqm1801([("005930", "2")], "      "),
    ])
    assert broker.sellable_quantity("005930") == Decimal(2)
    paths = [call.args[0] for call in client.post.call_args_list]
    assert paths == ["/api/v1/ssqm2952", "/api/v1/ssqm1801", "/api/v1/ssqm1801"]
    bodies = [call.kwargs["body"]["dataBody"] for call in client.post.call_args_list[1:]]
    assert bodies == [{"mkt_tm_ccd": "1", "inq_clsf": "0", "is_no": "", "nxt_key": ""},
                      {"mkt_tm_ccd": "1", "inq_clsf": "0", "is_no": "", "nxt_key": "NEXT-1"}]


def test_kb_kr_truncated_snapshot_not_held_is_zero_and_positions_stay_refused():
    broker, _ = _kb([_ssqm2952([], 21), _ssqm1801([("000660", "1")], "")])
    assert broker.sellable_quantity("005930") == Decimal(0)
    broker, _ = _kb([_ssqm2952([], 21)])
    with pytest.raises(SpecUnverifiedError, match="truncated"):
        broker.get_positions()


def test_kb_kr_sellable_listing_page_bound_is_refused():
    pages = [_ssqm1801([("000660", "1")], f"K{index}") for index in range(100)]
    broker, _ = _kb([_ssqm2952([], 21), *pages])
    with pytest.raises(InvalidResponseError, match="bounded"):
        broker.sellable_quantity("005930")
