from decimal import Decimal

import pytest

from pykbroker.exceptions import BrokerRejectedError, DryRunError, DuplicateOrderError, InvalidResponseError
from pykbroker.models import CancelRequest
from pykbroker.safety.gate import OrderSafety


def test_dbfi_recorded_contract(dbfi):
    broker, client, _ = dbfi
    assert broker.get_quote("A005930").ask == Decimal("70100")
    client.get_stock_price.assert_called_with(region="domestic", stock_code="005930", market_code="J")
    assert broker.get_balance().cash_available == Decimal("200000")
    assert broker.get_positions()[0].quantity == 2
    orders = broker.list_orders("005930")
    assert len(orders) == 1 and orders[0].filled == 1 and orders[0].status == "partial"
    assert broker.list_orders("000660") == []
    assert broker.capabilities().supports_single_order_cancel


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("venue", ["KRX", "NXT"])
def test_dbfi_exact_order_mapping(dbfi, request_order, side, venue):
    broker, client, _ = dbfi
    order = broker.place_order(request_order.model_copy(update={"side": side, "venue": venue}))
    assert order.id == "43"
    getattr(client, side).assert_called_once_with(
        region="domestic", stock_code="005930", quantity=1, price=70000, price_type="00", use_nxt=venue == "NXT"
    )
    getattr(client, "buy" if side == "sell" else "sell").assert_not_called()


def test_dbfi_cancel_exact_quantity_no_retry(dbfi):
    broker, client, _ = dbfi
    client.cancel.return_value = {"rsp_cd": "99999"}
    request = CancelRequest(order_id="42", symbol="005930", quantity=2, client_key="cancel-once")
    with pytest.raises(BrokerRejectedError):
        broker.cancel_order(request)
    with pytest.raises(DuplicateOrderError):
        broker.cancel_order(request)
    client.cancel.assert_called_once_with(
        region="domestic", order_no=42, stock_code="005930", quantity=2, use_nxt=False
    )


def test_dbfi_cancel_ack_does_not_invent_final_state(dbfi):
    broker, _, _ = dbfi
    result = broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    assert result.status == "accepted" and result.filled is None


def test_dbfi_dry_run_blocks_place_and_cancel(dbfi, request_order):
    broker, client, _ = dbfi
    broker.safety = OrderSafety()
    with pytest.raises(DryRunError):
        broker.place_order(request_order)
    with pytest.raises(DryRunError):
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    client.buy.assert_not_called()
    client.cancel.assert_not_called()


@pytest.mark.parametrize("response", [{}, [{"Out1": []}, {}], {"rsp_cd": "failed", "Out1": []}])
def test_dbfi_malformed_pages_fail(dbfi, response):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = response
    with pytest.raises((InvalidResponseError, BrokerRejectedError)):
        broker.list_orders()


def test_dbfi_zero_remaining_not_a_fill_claim(dbfi):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {
        "Out1": [{"OrdNo": "42", "IsuNo": "A005930", "AllExecQty": "0", "MrcAbleQty": "0"}]
    }
    assert broker.list_orders()[0].status == "unknown"


@pytest.mark.parametrize(
    "filled,remaining,quantity,status", [(1, 1, 2, "partial"), (2, 0, 2, "filled"), (1, 0, 2, "unknown")]
)
def test_dbfi_kr_execution_average_and_complete_quantity(dbfi, filled, remaining, quantity, status):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out1": [{
        "OrdNo": "42", "IsuNo": "A005930", "OrdQty": quantity,
        "AllExecQty": filled, "MrcAbleQty": remaining,
        "AvrExecPrc": "69900", "OrdPrc": "70000",
    }]}
    order = broker.list_orders()[0]
    assert order.fill_price == Decimal("69900")
    assert order.quantity == quantity and order.status == status


@pytest.mark.parametrize("average", [None, "0.00", ""])
def test_dbfi_missing_execution_price_never_uses_limit(dbfi, average):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"Out1": [{
        "OrdNo": "42", "IsuNo": "A005930", "AllExecQty": 1, "MrcAbleQty": 0,
        "OrdQty": 1, "AvrExecPrc": average, "OrdPrc": "70000",
    }]}
    assert broker.list_orders()[0].fill_price is None


def test_dbfi_cash_requeries_orderable_amount_and_ignores_d2_deposit(dbfi):
    broker, client, _ = dbfi
    client.get_able_order_quantity.return_value = {
        "rsp_cd": "00000", "Out": {"MgnRat100pctOrdAbleAmt": "100000", "Dps": "900000"}}
    assert broker.get_balance().cash_available == Decimal("100000")
    assert broker.available_cash(symbol="A005930", price=Decimal("70000")) == Decimal("100000")
    client.get_able_order_quantity.assert_called_with(
        region="domestic", stock_code="005930", order_type="2", price=70000)
    assert client.get_able_order_quantity.call_count == 2


@pytest.mark.parametrize("integrated,expected", [(False, "100"), (True, "300")])
def test_dbfi_us_cash_explicit_margin_selection(integrated, expected):
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {
        "AstkOrdAbleAmt": "100", "AstkOrdAbleAmt0": "300", "AstkOrdAbleAmt1": "900",
        "CsldtMgnUseYn": "Y"}}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=integrated)
    assert broker.available_cash(symbol="TEST", price=Decimal("120.50")) == Decimal(expected)
    client.get_able_order_quantity.assert_called_once_with(
        region="overseas", stock_code="TEST", order_type="2", price=120.5, won_fcurr_type="2")


@pytest.mark.parametrize("flag", ["N", None])
def test_dbfi_us_integrated_margin_requires_broker_confirmation(flag):
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {
        "AstkOrdAbleAmt0": "300", "CsldtMgnUseYn": flag}}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=True)
    with pytest.raises(InvalidResponseError, match="not confirmed"):
        broker.available_cash()


def test_dbfi_us_average_is_aggregate_execution_amount_in_usd():
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    row = {"OrdNo": "42", "SymCode": "TEST", "AstkOrdQty": 2,
           "AstkExecQty": 2, "AstkOrdRmqty": 0, "AstkExecAmt": "239", "CrcyCode": "USD",
           "AstkOrdPrc": "120.50"}
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out": [row]}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client)
    order = broker.list_orders()[0]
    assert order.fill_price == Decimal("119.5") and order.status == "filled"
    client.get_transaction_history.assert_called_once_with(
        region="overseas", execution_status="0", order_type="0", stock_code="", query_type="0", won_fcurr_type="2")
    row["CrcyCode"] = "KRW"
    with pytest.raises(InvalidResponseError, match="currency"):
        broker.list_orders()


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_dbfi_integrated_flag_cannot_be_inferred(flag):
    from pykbroker.brokers.dbfi import DBFI
    from pykbroker.exceptions import ConfigurationError
    with pytest.raises(ConfigurationError, match="explicit boolean"):
        DBFI(account="fixture", is_integrated=flag)


# CSPAQ04800 cancel evidence — field names and OrdTrxPtnCode values from DB증권's own spec
# (DBsecurities/dbsec-open-api examples/kr_stock_order/kr_stock_inquire_executions.py):
# OrgOrdNo 원주문번호 · MrcQty 정정취소수량 · OrdTrxPtnCode 0 정상 6 정정확인 8 취소확인 7 정정중 9 취소중.
def _kr_original(**extra):
    row = {"OrdNo": 42, "OrgOrdNo": 0, "IsuNo": "A005930", "OrdQty": 3, "AllExecQty": 1,
           "MrcAbleQty": 0, "AvrExecPrc": "70000", "OrdPrc": "70000", "OrdTrxPtnCode": 0}
    row.update(extra)
    return row


def _kr_cancel_row(code, qty=2):
    return {"OrdNo": 43, "OrgOrdNo": 42, "IsuNo": "A005930", "OrdQty": qty, "AllExecQty": 0,
            "MrcAbleQty": 0, "AvrExecPrc": "0", "OrdPrc": "0", "OrdTrxPtnCode": code}


@pytest.mark.parametrize(
    "rows",
    [
        [_kr_original(MrcQty=2), _kr_cancel_row(8)],
        [_kr_original(MrcQty=2), _kr_cancel_row("8")],
        [_kr_original(), _kr_cancel_row(8)],
        [_kr_original(MrcQty=2, OrdTrxPtnCode=8)],
        [_kr_cancel_row(8), _kr_original(MrcQty="2")],
    ],
)
def test_dbfi_kr_partial_fill_then_confirmed_cancel_is_terminal(dbfi, rows):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out1": rows}
    orders = broker.list_orders()
    assert [o.id for o in orders] == ["42"]  # the cancel order row is folded into its original
    order = orders[0]
    assert order.status == "cancelled" and order.filled == 1 and order.remaining == 0 and order.quantity == 3


def test_dbfi_kr_unfilled_order_confirmed_cancel_is_cancelled(dbfi):
    broker, client, _ = dbfi
    rows = [_kr_original(AllExecQty=0, MrcQty=3), _kr_cancel_row(8, qty=3)]
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out1": rows}
    order = broker.list_orders()[0]
    assert order.status == "cancelled" and order.filled == 0


@pytest.mark.parametrize(
    "rows",
    [
        [_kr_original(MrcQty=2), _kr_cancel_row(9)],                       # 취소중
        [_kr_original(MrcQty=2, OrdTrxPtnCode=9)],                         # 취소중 on the order
        [_kr_original(MrcQty=2, OrdTrxPtnCode=7)],                         # 정정중
        [_kr_original(MrcQty=2), _kr_cancel_row(8), _kr_cancel_row(9)],    # a second cancel still in flight
        [_kr_original(MrcQty=2), _kr_cancel_row(None)],                     # unclassified linked row
        [_kr_original(MrcQty=2)],                                           # no confirmation anywhere
        [_kr_original()],                                                   # quantities alone
        [_kr_original(MrcQty=1), _kr_cancel_row(8)],                        # reported cancel ≠ rest
        [_kr_original(), _kr_cancel_row(8, qty=1)],                         # cancel row ≠ rest
        [_kr_original(OrdTrxPtnCode=8)],                                    # confirmed but no quantity
        [_kr_original(MrcAbleQty=1, MrcQty=1), _kr_cancel_row(8, qty=1)],  # rest still cancellable
        [{k: v for k, v in _kr_original(MrcQty=2).items() if k != "OrdQty"}, _kr_cancel_row(8)],
    ],
)
def test_dbfi_kr_ambiguous_cancel_never_terminal(dbfi, rows):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out1": rows}
    order = next(o for o in broker.list_orders() if o.id == "42")
    assert order.status != "cancelled" and order.status != "filled"
    assert order.filled == 1


def test_dbfi_kr_cancel_row_is_found_on_another_page(dbfi):
    broker, client, _ = dbfi
    client.get_transaction_history.return_value = [
        {"rsp_cd": "00000", "Out1": [_kr_original(MrcQty=2)]},
        {"rsp_cd": "00000", "Out1": [_kr_cancel_row(8)]},
    ]
    assert broker.list_orders()[0].status == "cancelled"


# CAZCQ00100 (US) cancel evidence — field names from DB증권's own spec
# (DBsecurities/dbsec-open-api examples/ov_stock_order/ov_stock_inquire_executions.py): OrdNo · OrgOrdNo 원주문번호 ·
# AstkOrdQty · AstkExecQty · AstkOrdRmqty 주문잔량 · OrdTrdTpCode 주문거래구분코드 · AstkRjtCode · AstkRjtRsnCnts.
# OrdTrdTpCode 0 주문 · 1 정정주문 · 2 취소주문 (examples/ov_stock_order/ov_stock_order.py CAZCT00100 In;
# examples/ov_stock_realtime/ov_stock_realtime_order_execution.py IS2 Sordtrdtpcode). AstkOrdStatCode has no
# published value table and is deliberately absent/irrelevant here: the numbers decide.
def _us_original(**extra):
    row = {"OrdNo": 42, "OrgOrdNo": 0, "OrdTrdTpCode": "0", "SymCode": "AAPL", "AstkOrdQty": "3",
           "AstkExecQty": "1", "AstkOrdRmqty": "0", "AstkExecAmt": "190.00", "CrcyCode": "USD",
           "AstkRjtCode": "", "AstkRjtRsnCnts": "", "AstkOrdStatCode": "??"}
    row.update(extra)
    return row


def _us_cancel_row(qty=2, no=43, parent=42, **extra):
    row = {"OrdNo": no, "OrgOrdNo": parent, "OrdTrdTpCode": "2", "SymCode": "AAPL", "AstkOrdQty": str(qty),
           "AstkExecQty": "0", "AstkOrdRmqty": "0", "AstkExecAmt": "0", "CrcyCode": "USD",
           "AstkRjtCode": "", "AstkRjtRsnCnts": "", "AstkOrdStatCode": "??"}
    row.update(extra)
    return row


def _us_broker(response):
    from unittest.mock import Mock

    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_transaction_history.return_value = response
    return DBFI(account="fixture", market="US", venue="NASDAQ", client=client), client


def _us_orders(rows):
    broker, _ = _us_broker({"rsp_cd": "00000", "Out": rows})
    return broker.list_orders()


@pytest.mark.parametrize(
    "rows",
    [
        [_us_original(AstkExecQty="0", AstkExecAmt="0"), _us_cancel_row(qty=3)],
        [_us_cancel_row(qty=3), _us_original(AstkExecQty="0", AstkExecAmt="0")],
        [_us_original(AstkExecQty=0, AstkOrdQty=3, AstkOrdRmqty=0, OrdTrdTpCode=0),
         _us_cancel_row(qty=3, OrdTrdTpCode=2, AstkOrdRmqty=0, AstkExecQty=3)],  # ints · cancel row execs its qty
    ],
    ids=["after", "before", "ints"],
)
def test_dbfi_us_zero_fill_cancel_is_cancelled(rows):
    orders = _us_orders(rows)
    assert [o.id for o in orders] == ["42"]  # the cancel order row is folded into its original
    order = orders[0]
    assert (order.status, order.filled, order.remaining, order.quantity, order.fill_price) == ("cancelled", 0, 0, 3, None)


def test_dbfi_us_partial_fill_then_remainder_cancel_is_cancelled_with_fill():
    [order] = _us_orders([_us_original(), _us_cancel_row(qty=2)])
    assert (order.status, order.filled, order.remaining, order.quantity) == ("cancelled", 1, 0, 3)
    assert order.fill_price == Decimal("190")


@pytest.mark.parametrize(
    "rows",
    [
        # T's TTL expiry (expire_due_intents) sends the same CAZCT00100 cancel for the whole rest.
        [_us_original(AstkExecQty="0", AstkExecAmt="0", AstkOrdQty="5"), _us_cancel_row(qty=5)],
        # a customer cancel of 1 then the TTL cancel of the last 1 after 1 filled: 1 + 1 + 1 == 3
        [_us_original(), _us_cancel_row(qty=1, no=43), _us_cancel_row(qty=1, no=44)],
    ],
    ids=["ttl-zero-fill", "two-cancels"],
)
def test_dbfi_us_expiry_ttl_cancel_is_cancelled(rows):
    orders = _us_orders(rows)
    assert [o.id for o in orders] == ["42"] and orders[0].status == "cancelled"
    assert orders[0].filled + sum(int(r["AstkOrdQty"]) for r in rows if r["OrdTrdTpCode"] == "2") == orders[0].quantity


@pytest.mark.parametrize(
    "rows",
    [
        [_us_original(AstkExecQty="3", AstkExecAmt="570.00")],
        # the cancel lost the race: the venue rejected it and the order filled — numbers win
        [_us_original(AstkExecQty="3", AstkExecAmt="570.00"),
         _us_cancel_row(qty=2, AstkRjtCode="1234", AstkRjtRsnCnts="취소가능수량 없음")],
    ],
    ids=["plain", "cancel-rejected"],
)
def test_dbfi_us_full_fill_is_filled(rows):
    orders = _us_orders(rows)
    assert [o.id for o in orders] == ["42"]
    assert (orders[0].status, orders[0].filled, orders[0].fill_price) == ("filled", 3, Decimal("190"))


def test_dbfi_us_reject_needs_code_and_reason():
    rejected = dict(AstkExecQty="0", AstkExecAmt="0", AstkRjtCode="1234", AstkRjtRsnCnts="주문가능금액 부족")
    assert _us_orders([_us_original(**rejected)])[0].status == "rejected"
    for shape in (dict(rejected, AstkRjtCode="0000"), dict(rejected, AstkRjtCode=""),
                  dict(rejected, AstkRjtRsnCnts=" "), dict(rejected, AstkOrdRmqty="3"),
                  dict(rejected, AstkExecQty="1", AstkExecAmt="190")):
        assert _us_orders([_us_original(**shape)])[0].status not in ("rejected", "cancelled", "filled"), shape


@pytest.mark.parametrize(
    "rows",
    [
        [_us_original()],                                                        # venue expiry: no cancel evidence
        [_us_original(), _us_cancel_row(qty=2, AstkOrdRmqty="2")],               # cancel still pending
        [_us_original(), _us_cancel_row(qty=1)],                                 # cancelled ≠ rest
        [_us_original(), _us_cancel_row(qty=3)],                                 # cancelled + filled > ordered
        [_us_original(), _us_cancel_row(qty=2, AstkRjtCode="1234", AstkRjtRsnCnts="거부")],  # cancel rejected
        [_us_original(), _us_cancel_row(qty=2, OrdTrdTpCode="1")],               # 정정, not 취소
        [_us_original(), _us_cancel_row(qty=2, OrdTrdTpCode=None)],              # unclassified linked row
        [_us_original(), _us_cancel_row(qty=2, AstkExecQty="1")],                # cancel row disagrees with itself
        [_us_original(), _us_cancel_row(qty=2, AstkOrdQty="")],                  # cancel quantity missing
        [_us_original(), _us_cancel_row(qty=2, AstkOrdRmqty=None)],              # cancel remainder missing
        [_us_original(), _us_cancel_row(qty=2), _us_cancel_row(qty=2, no=44)],   # double count
        [_us_original(AstkRjtCode="9", AstkRjtRsnCnts="거부"), _us_cancel_row(qty=2)],  # original carries a reject
        [{k: v for k, v in _us_original().items() if k != "AstkOrdQty"}, _us_cancel_row(qty=2)],
    ],
)
def test_dbfi_us_unproven_cancel_never_terminal(rows):
    order = next(o for o in _us_orders(rows) if o.id == "42")
    assert order.status not in ("cancelled", "filled", "rejected")
    assert order.filled == 1


def test_dbfi_us_remaining_shares_stay_open_or_partial():
    assert _us_orders([_us_original(AstkOrdRmqty="2")])[0].status == "partial"
    open_ = _us_orders([_us_original(AstkExecQty="0", AstkExecAmt="0", AstkOrdRmqty="3"), _us_cancel_row(qty=3)])
    assert [o.id for o in open_] == ["42"] and open_[0].status == "open"  # cancel sent, venue has not taken it yet


def test_dbfi_us_cancel_of_cancel_is_folded_and_never_terminal():
    rows = [_us_original(), _us_cancel_row(qty=2), _us_cancel_row(qty=2, no=44, parent=43)]
    orders = _us_orders(rows)
    assert [o.id for o in orders] == ["42"]
    assert orders[0].status not in ("cancelled", "filled") and orders[0].filled == 1


@pytest.mark.parametrize("parent", [43, "43", 0, "", None])
def test_dbfi_us_self_ref_or_unlinked_cancel_row_is_no_proof(parent):
    stray = _us_cancel_row(qty=2, no=43, parent=parent, AstkExecQty="2")
    orders = {o.id: o for o in _us_orders([_us_original(), stray])}
    assert orders["42"].status not in ("cancelled", "filled") and orders["42"].filled == 1
    assert orders["43"].status == "unknown"  # never «filled» from a cancel order's own numbers


def test_dbfi_us_late_fill_after_confirmed_cancel():
    """F2 D520: a cancel is confirmed, then a later inquiry shows a fill for part of the cancelled quantity."""
    broker, client = _us_broker({"rsp_cd": "00000", "Out": [
        _us_original(AstkExecQty="0", AstkExecAmt="0"), _us_cancel_row(qty=3)]})
    first = broker.list_orders()
    assert [(o.id, o.status, o.filled) for o in first] == [("42", "cancelled", 0)]

    # The venue's own cumulative AstkExecQty now says 1; the cancel row still says 3 → 1 + 3 ≠ 3: no longer proven.
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out": [
        _us_original(AstkExecQty="1", AstkExecAmt="190.00"), _us_cancel_row(qty=3)]}
    for _ in range(2):  # the same reply read twice never adds the fill twice
        late = broker.list_orders()
        assert [o.id for o in late] == ["42"]  # the cancel never reappears as an order of its own
        assert late[0].filled == 1 and late[0].fill_price == Decimal("190")
        assert late[0].status not in ("accepted", "open", "partial", "filled")  # a closed cancel never reopens

    # DB corrects the cancelled quantity to the 2 it really cancelled → proven again, with the true fill.
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out": [
        _us_original(AstkExecQty="1", AstkExecAmt="190.00"), _us_cancel_row(qty=2)]}
    assert [(o.id, o.status, o.filled, o.remaining) for o in broker.list_orders()] == [("42", "cancelled", 1, 0)]

    # The late fills took every share: numbers win over the cancel row.
    client.get_transaction_history.return_value = {"rsp_cd": "00000", "Out": [
        _us_original(AstkExecQty="3", AstkExecAmt="570.00"), _us_cancel_row(qty=3)]}
    assert [(o.id, o.status, o.filled) for o in broker.list_orders()] == [("42", "filled", 3)]


def test_dbfi_us_cancel_row_is_found_on_another_page():
    broker, _ = _us_broker([
        {"rsp_cd": "00000", "Out": [_us_original()]},
        {"rsp_cd": "00000", "Out": [_us_cancel_row(qty=2)]},
    ])
    assert [(o.id, o.status, o.filled) for o in broker.list_orders()] == [("42", "cancelled", 1)]


def test_dbfi_us_symbol_filter_still_folds_cancel_rows():
    broker, _ = _us_broker({"rsp_cd": "00000", "Out": [_us_original(), _us_cancel_row(qty=2)]})
    assert [(o.id, o.status) for o in broker.list_orders("AAPL")] == [("42", "cancelled")]
