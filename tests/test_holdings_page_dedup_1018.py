"""Holdings pages never count one vendor row twice (trader #527/#528 review 527b P1-1).

Holding 2 shares listed again on the next page must still read as 2: a 3-share sell is refused
before the broker, a 2-share sell passes. A repeat that disagrees with the earlier page is refused
(caller: «holdings unknown», nothing sent). Distinct rows of one symbol (another venue, balance
type or loan) stay distinct. Synthetic responses shaped by the official KIS/LS/KB contracts.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers import kis as kis_module
from pykbroker.brokers._common import PageRows
from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.exceptions import InvalidResponseError
from tests.test_sellable_pagination_1018 import (
    _cosoq,
    _kb,
    _kb_envelope,
    _kis,
    _kis_row,
    _ls,
    _ssqm1801,
    _ssqm2952,
    _t0424,
)


def _sellable(positions, symbol):
    return sum(p.sellable for p in positions if p.symbol == symbol)


def _kis_first(rows, flag):
    return {"json_data": {"rt_cd": "0", "output1": rows}, "tr_cont": flag}


def _kis_page(rows):
    return {"rt_cd": "0", "output1": rows}


# ------------------------------------------------------------------ PageRows itself


def test_page_rows_identical_repeat_counts_once_and_conflict_is_refused():
    pages = PageRows(("symbol",), "Fixture")
    assert pages.add([{"symbol": "AAPL", "qty": "2"}]) == [{"symbol": "AAPL", "qty": "2"}]
    assert pages.add([{"symbol": "AAPL", "qty": "2"}, {"symbol": "MSFT", "qty": "1"}]) == [
        {"symbol": "MSFT", "qty": "1"}]
    assert [row["symbol"] for row in pages.rows] == ["AAPL", "MSFT"]
    with pytest.raises(InvalidResponseError, match="different values"):
        pages.add([{"symbol": "AAPL", "qty": "3"}])


def test_page_rows_keeps_rows_within_one_page_as_sent():
    pages = PageRows(("symbol",), "Fixture")
    rows = [{"symbol": "AAPL", "lot": 1}, {"symbol": "AAPL", "lot": 2}]
    assert pages.add(rows) == rows
    # A later page repeating either one exactly is the same row again.
    assert pages.add([{"symbol": "AAPL", "lot": 2}]) == []
    assert len(pages.rows) == 2


# ------------------------------------------------------------------ KIS US (CTRP6504R)


def test_kis_us_duplicated_page_holding_two_reads_two():
    row = _kis_row("AAPL", "2", "2")
    broker, _ = _kis(_kis_first([row], "F"), [(_kis_page([dict(row)]), "D")])
    positions = broker.get_positions()
    assert _sellable(positions, "AAPL") == 2
    assert [p.symbol for p in positions] == ["AAPL"]


def test_kis_us_overlapping_pages_keep_each_row_once():
    aapl = _kis_row("AAPL", "2", "2")
    broker, _ = _kis(_kis_first([_kis_row("MSFT", "1", "1"), aapl], "F"),
                     [(_kis_page([dict(aapl), _kis_row("NVDA", "5", "4")]), "D")])
    positions = broker.get_positions()
    assert [p.symbol for p in positions] == ["MSFT", "AAPL", "NVDA"]
    assert (_sellable(positions, "AAPL"), _sellable(positions, "NVDA")) == (2, 4)


def test_kis_us_conflicting_repeat_is_refused():
    broker, _ = _kis(_kis_first([_kis_row("AAPL", "2", "2")], "F"),
                     [(_kis_page([_kis_row("AAPL", "4", "4")]), "D")])
    with pytest.raises(InvalidResponseError, match="different values"):
        broker.get_positions()


def test_kis_us_same_symbol_on_another_exchange_stays_distinct():
    nasdaq = {**_kis_row("AAPL", "2", "2"), "ovrs_excg_cd": "NASD"}
    nyse = {**_kis_row("AAPL", "1", "1"), "ovrs_excg_cd": "NYSE"}
    broker, _ = _kis(_kis_first([nasdaq], "F"), [(_kis_page([nyse]), "D")])
    assert _sellable(broker.get_positions(), "AAPL") == 3


def test_kis_us_empty_next_page_ends_the_list():
    broker, scope = _kis(_kis_first([_kis_row("AAPL", "2", "2")], "F"), [(_kis_page([]), "D")])
    assert _sellable(broker.get_positions(), "AAPL") == 2
    assert scope.client.request.call_count == 1


def test_kis_us_repeating_page_loop_is_refused_not_summed():
    row = _kis_row("AAPL", "2", "2")
    broker, scope = _kis(_kis_first([row], "M"), [(_kis_page([dict(row)]), "M")] * kis_module.KIS_MAX_PAGES)
    with pytest.raises(InvalidResponseError, match="page limit"):
        broker.get_positions()
    assert scope.client.request.call_count == kis_module.KIS_MAX_PAGES - 1


def test_kis_us_fractional_repeat_is_refused_never_rounded_or_doubled():
    row = _kis_row("AAPL", "2.57", "2.57")
    broker, _ = _kis(_kis_first([row], "F"), [(_kis_page([dict(row)]), "D")])
    with pytest.raises(InvalidResponseError, match="Fractional"):
        broker.get_positions()


# ------------------------------------------------------------------ LS KR (t0424) / LS US (COSOQ00201)


def test_ls_kr_duplicated_page_holding_two_reads_two():
    broker, _ = _ls([
        (_t0424([("005930", "2", "2")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"}),
        (_t0424([("005930", "2", "2")], ""), {"tr_cont": "N", "tr_cont_key": ""}),
    ])
    positions = broker.get_positions()
    assert [p.symbol for p in positions] == ["005930"]
    assert _sellable(positions, "005930") == 2


def test_ls_kr_overlapping_pages_and_conflict():
    broker, _ = _ls([
        (_t0424([("000660", "1", "1"), ("005930", "2", "2")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"}),
        (_t0424([("005930", "2", "2"), ("035720", "4", "3")], ""), {"tr_cont": "N", "tr_cont_key": ""}),
    ])
    positions = broker.get_positions()
    assert [p.symbol for p in positions] == ["000660", "005930", "035720"]
    assert _sellable(positions, "005930") == 2
    broker, _ = _ls([
        (_t0424([("005930", "2", "2")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"}),
        (_t0424([("005930", "4", "4")], ""), {"tr_cont": "N", "tr_cont_key": ""}),
    ])
    with pytest.raises(InvalidResponseError, match="different values"):
        broker.get_positions()


def test_ls_kr_other_balance_type_on_next_page_stays_distinct():
    first = _t0424([("005930", "2", "2")], "005930")
    second = _t0424([("005930", "1", "1")], "")
    second["t0424OutBlock1"][0]["jangb"] = first["t0424OutBlock1"][0].get("jangb", "") + "-loan"
    broker, _ = _ls([(first, {"tr_cont": "Y", "tr_cont_key": "k1"}), (second, {"tr_cont": "N", "tr_cont_key": ""})])
    assert _sellable(broker.get_positions(), "005930") == 3


def test_ls_kr_empty_next_page_and_cursor_loop():
    broker, _ = _ls([
        (_t0424([("005930", "2", "2")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"}),
        (_t0424([], ""), {"tr_cont": "N", "tr_cont_key": ""}),
    ])
    assert _sellable(broker.get_positions(), "005930") == 2
    loop = (_t0424([("005930", "2", "2")], "005930"), {"tr_cont": "Y", "tr_cont_key": "k1"})
    broker, _ = _ls([loop, loop, loop])
    with pytest.raises(InvalidResponseError, match="(?i)repeated"):
        broker.get_positions()


def test_ls_us_duplicated_page_holding_two_reads_two_and_conflict_is_refused():
    broker, _ = _ls([
        (_cosoq("AAPL", 2, 2), {"tr_cont": "Y", "tr_cont_key": "next-1"}),
        (_cosoq("AAPL", 2, 2), {"tr_cont": "N", "tr_cont_key": ""}),
    ], market="US", venue="NASDAQ")
    positions = broker.get_positions()
    assert [p.symbol for p in positions] == ["AAPL"] and _sellable(positions, "AAPL") == 2
    broker, _ = _ls([
        (_cosoq("AAPL", 2, 2), {"tr_cont": "Y", "tr_cont_key": "next-1"}),
        (_cosoq("AAPL", 4, 4), {"tr_cont": "N", "tr_cont_key": ""}),
    ], market="US", venue="NASDAQ")
    with pytest.raises(InvalidResponseError, match="different values"):
        broker.get_positions()


def test_ls_us_empty_next_page_ends_the_list():
    empty = _cosoq("AAPL", 2, 2)
    empty["COSOQ00201OutBlock4"] = []
    broker, _ = _ls([
        (_cosoq("AAPL", 2, 2), {"tr_cont": "Y", "tr_cont_key": "next-1"}),
        (empty, {"tr_cont": "N", "tr_cont_key": ""}),
    ], market="US", venue="NASDAQ")
    assert _sellable(broker.get_positions(), "AAPL") == 2


# ------------------------------------------------------------------ KB KR (SSQM1801) / KB US (SPQM2226)


def test_kb_kr_duplicated_sellable_page_holding_two_reads_two():
    broker, _ = _kb([
        _ssqm2952([("000660", "1", "1")], 21),
        _ssqm1801([("005930", "2")], "NEXT-1"),
        _ssqm1801([("005930", "2")], ""),
    ])
    assert broker.sellable_quantity("005930") == Decimal(2)


def test_kb_kr_overlap_conflict_empty_page_and_cursor_loop():
    broker, _ = _kb([
        _ssqm2952([], 21),
        _ssqm1801([("000660", "1"), ("005930", "2")], "NEXT-1"),
        _ssqm1801([("005930", "2"), ("035720", "3")], ""),
    ])
    assert broker.sellable_quantity("005930") == Decimal(2)
    broker, _ = _kb([_ssqm2952([], 21), _ssqm1801([("005930", "2")], "NEXT-1"), _ssqm1801([("005930", "4")], "")])
    with pytest.raises(InvalidResponseError, match="different values"):
        broker.sellable_quantity("005930")
    broker, _ = _kb([_ssqm2952([], 21), _ssqm1801([("005930", "2")], "NEXT-1"), _ssqm1801([], "")])
    assert broker.sellable_quantity("005930") == Decimal(2)
    broker, _ = _kb([_ssqm2952([], 21), _ssqm1801([("005930", "2")], "NEXT-1"),
                     _ssqm1801([("005930", "2")], "NEXT-1")])
    with pytest.raises(InvalidResponseError, match="repeated"):
        broker.sellable_quantity("005930")


def _spqm2226(rows, nxt):
    return _kb_envelope({"nxt_key": nxt, "grid_cnt2": str(len(rows)), "Record2": [
        {"crncy_clsf_nm": "USD", "is_cd": symbol, "is_nm": "fixture", "frgn_hld_q_p6": held,
         "frgn_ordr_psbl_q_p6": sellable, "now_prc_p4": "120", "byng_avr_prc_p4": "100"}
        for symbol, held, sellable in rows]})


def _kb_us(responses):
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.side_effect = responses
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
                market="US", venue="NASDAQ", client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    return broker


def test_kb_us_duplicated_page_holding_two_reads_two_and_conflict_is_refused():
    broker = _kb_us([_spqm2226([("AAPL", "2", "2")], "NEXT-1"), _spqm2226([("AAPL", "2", "2")], "")])
    assert broker.sellable_quantity("AAPL") == Decimal(2)
    broker = _kb_us([_spqm2226([("AAPL", "2", "2")], "NEXT-1"), _spqm2226([("AAPL", "4", "4")], "")])
    with pytest.raises(InvalidResponseError, match="different values"):
        broker.sellable_quantity("AAPL")
