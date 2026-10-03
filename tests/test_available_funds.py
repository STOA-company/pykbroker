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
    broker, client, _ = dbfi
    client.get_able_order_quantity.return_value["Out"]["MgnRat100pctOrdAbleAmt"] = "250000"
    assert broker.available_cash() == Decimal(250000)
    assert broker.available_qty("005930") == 1
    balance = broker.get_balance()
    assert balance.cash_available == Decimal(200000)
    assert balance.total == Decimal(340000)


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



def test_kb_uses_broker_integrated_amount_without_local_fx():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                              "dataBody": {"fcrncy_unty_ordr_psbl_amt_p2": "123.45"}}
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", is_integrated=True, client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    assert broker.orderable_cash("TSLA") == Decimal("123.45")
    assert client.post.call_args.kwargs["body"]["dataBody"] == {"stnd_is_cd": "TSLA"}
    broker._is_integrated = False
    from pykbroker.exceptions import SpecUnverifiedError
    with pytest.raises(SpecUnverifiedError):
        broker.orderable_cash("TSLA")
    assert client.post.call_count == 1



def test_kb_us_balance_preserves_native_cash_without_integrated_capacity():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    header = {"resultCode": "200", "processFlag": "A"}
    client.post.side_effect = [
        {"dataHeader": header, "dataBody": {"grid_cnt": "1", "Record1": [
            {"crncy_cd": "USD", "fcrncy_unty_ordr_psbl_amt_p2": "999.99",
             "fcrncy_tfnd": "123.45", "fcrncy_use_psbl_amt_p2": "123.45"}]}},
        {"dataHeader": header, "dataBody": {"grid_cnt2": "0", "Record2": [], "nxt_key": ""}},
    ]
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", is_integrated=False, client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    balance = broker.get_balance()
    assert balance.currency == "USD"
    assert balance.cash_available == Decimal("123.45")
    assert balance.position_value == 0
    assert client.post.call_count == 2


def test_kb_truncated_domestic_holdings_never_become_empty():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    from pykbroker.exceptions import SpecUnverifiedError
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                              "dataBody": {"tl_data_cnt": "21", "grid_cnt1": "0", "Record1": []}}
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token", client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    with pytest.raises(SpecUnverifiedError, match="truncated"):
        broker.get_positions()



def test_kb_exact_us_ledger_preserves_partial_fill_and_continuation():
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    from pykbroker.models import Order
    header = {"resultCode": "200", "processFlag": "A"}
    row = {"ordr_no": "42", "crncy_cd": "USD", "frex_ccd": "NAS", "shrt_is_cd": "TSLA",
           "frgn_ordr_q_p6": "3.000000", "ccls_q_p6": "1.000000", "nccls_q_p6": "2.000000",
           "frgn_ordr_prc_p6": "10.500000", "frgn_ccls_prc_p6": "10.450000", "etc_trd_ccd": "02"}
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.side_effect = [
        {"dataHeader": header, "dataBody": {"grid_cnt": "1", "Record1": [row], "nxt_key": "next"}},
        {"dataHeader": header, "dataBody": {"grid_cnt": "0", "Record1": [], "nxt_key": ""}},
    ]
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", client=client, order_date=date(2026, 10, 2))
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    answer = broker.status(Order(id="42", symbol="TSLA", market="US"))
    assert answer.status == "partial"
    assert answer.filled == 1 and answer.remaining == 2
    assert answer.price == Decimal("10.5") and answer.fill_price == Decimal("10.45")
    assert client.post.call_args.kwargs["body"]["dataBody"]["nxt_key"] == "next"
    assert client.post.call_args.kwargs["body"]["dataBody"]["strt_ordr_dt"] == "20261002"


def test_kb_repeated_ledger_cursor_never_returns_partial_snapshot():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                              "dataBody": {"grid_cnt": "0", "Record1": [], "nxt_key": "loop"}}
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    with pytest.raises(InvalidResponseError, match="repeated"):
        broker.list_orders()
    assert client.post.call_count == 2

@pytest.mark.parametrize("broker_type", ["kis", "dbfi"])
def test_us_native_balance_excludes_integrated_buying_power(broker_type):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    from pykbroker.brokers.kis import KIS
    client, api, scope = Mock(), Mock(), Mock()
    if broker_type == "kis":
        client.key = SimpleNamespace(virtual_account=False)
        client.account.return_value = scope
        api.get_balances.return_value = {"rt_cd": "0", "output1": [], "output2": [
            {"crcy_cd": "USD", "frcr_dncl_amt_2": "100", "frcr_drwg_psbl_amt_1": "80"}]}
        broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api,
                     is_integrated=True)
    else:
        client.get_stock_balance.return_value = [{"rsp_cd": "00000", "Out2": [], "Out1": [
            {"CrcyCode": "USD", "FcurrDps": "100", "AstkMnyoutAbleAmt": "80",
             "CrcyOrdAbleAmt0": "9999"}]}]
        broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=True)
    balance = broker.get_balance()
    assert balance.cash_available == Decimal("80") and balance.total == Decimal("80")

def test_kb_us_cancel_does_not_require_a_new_integrated_buy_policy():
    from pykbroker.brokers.kb import KB
    from pykbroker.exceptions import SpecUnverifiedError
    from pykbroker.models import CancelRequest, OrderRequest
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", is_integrated=False, writes_enabled=True)
    broker._validate_order(CancelRequest(order_id="42", symbol="AAPL", market="US", venue="NASDAQ",
                                        quantity=1, client_key="cancel-fixture"))
    with pytest.raises(SpecUnverifiedError, match="Global One Market"):
        broker._validate_order(OrderRequest(symbol="AAPL", market="US", venue="NASDAQ",
                                           side="buy", quantity=1, price="100", client_key="buy-fixture"))

@pytest.mark.parametrize("standard,short,expected", [("KR7005930003", "005930", "005930"),
    ("KR7005930004", "005930", None), (None, "005930", None), ("KR7005930003", None, None)])
def test_kb_standard_instrument_uses_exact_native_mapping(standard, short, expected):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
        "dataBody": {"stnd_is_cd": standard, "shrt_is_cd": short}}
    broker = KB(account="fixture", bound_account="fixture", app_key="fake", token="fake", client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    if expected is None:
        with pytest.raises(InvalidResponseError):
            broker._response_symbol("KR7005930003", "KR")
    else:
        assert broker._response_symbol("KR7005930003", "KR") == expected
        assert broker._response_symbol("KR7005930003", "KR") == expected
        assert client.post.call_count == 1
    assert client.post.call_args.args[0] == "/api/v1/siqm4900"
    assert client.post.call_args.kwargs["body"]["dataBody"] == {"stnd_is_cd": "KR7005930003"}
