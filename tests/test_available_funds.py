"""A′-3: available_cash/available_qty are the settlement-aware numbers P-1 polls between orders."""

from decimal import Decimal

import pytest

from pykbroker.exceptions import InvalidResponseError


def test_kis_available_cash_is_the_orderable_cash_not_the_total(kis):
    broker, _, _, scope = kis
    # Fixture: orderable cash 200,000 while positions are worth another 140,000.
    assert broker.available_cash() == Decimal(200000)
    assert broker.get_balance().total == Decimal(340000)


def test_kis_available_qty_is_the_sellable_not_the_held_quantity(kis):
    broker, _, _, _ = kis
    # Fixture: 2 shares held, only 1 of them settled and sellable today.
    assert broker.available_qty("005930") == 1
    assert broker.get_positions()[0].quantity == 2


def test_dbfi_answers_the_same_two_questions(dbfi):
    broker, _, _ = dbfi
    assert broker.available_cash() == Decimal(200000)
    assert broker.available_qty("005930") == 1


@pytest.mark.parametrize("symbol", ["A005930", "005930"])
def test_available_qty_normalises_the_symbol(kis, symbol):
    assert kis[0].available_qty(symbol) == 1


def test_a_symbol_that_is_not_held_has_nothing_available(kis):
    assert kis[0].available_qty("000660") == 0


def test_every_call_re_reads_the_account(kis):
    """Bypass 1: a cached answer would defeat the whole point — the number changes as fills settle."""
    broker, _, _, scope = kis
    broker.available_cash()
    broker.available_cash()
    assert scope.amount.call_count == 2
    before = scope.balance_all.call_count
    broker.available_qty("005930")
    broker.available_qty("005930")
    assert scope.balance_all.call_count == before + 2


def test_more_sellable_than_held_is_refused_not_believed(kis):
    """Bypass 2: a vendor row claiming more sellable than held would over-sell the account."""
    broker, _, _, scope = kis
    scope.balance_all.return_value = {
        "evlu_amt_smtl_amt": "140000",
        "stocks": [
            {
                "pdno": "005930",
                "prdt_name": "Fixture equity",
                "hldg_qty": "2",
                "ord_psbl_qty": "3",
                "pchs_avg_pric": "68000",
                "prpr": "70000",
                "evlu_amt": "140000",
            }
        ],
    }
    with pytest.raises(InvalidResponseError):
        broker.available_qty("005930")


def test_available_qty_rejects_a_symbol_from_another_market(kis):
    with pytest.raises(Exception):
        kis[0].available_qty("AAPL")
