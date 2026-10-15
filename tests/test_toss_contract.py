import asyncio
from decimal import Decimal

import pytest

from pykbroker import CancelRequest, Toss
from pykbroker.exceptions import ConfigurationError, InvalidResponseError


class LoopSDK:
    def __init__(self, **kwargs):
        self.loop = asyncio.get_running_loop()
        self.calls = []
        self.closed = False

    async def get_prices(self, symbols):
        assert self.loop is asyncio.get_running_loop()
        return [{"symbol": symbols[0], "lastPrice": "70000"}]

    async def get_orderbook(self, symbol):
        assert self.loop is asyncio.get_running_loop()
        return {"asks": [{"price": "70100"}], "bids": [{"price": "69900"}]}

    async def get_buying_power(self, currency):
        return {"currency": currency, "cashBuyingPower": "100000"}

    async def get_positions(self):
        return {
            "marketValue": {"amount": {"krw": "140000", "usd": "0"}},
            "items": [
                {
                    "symbol": "005930",
                    "name": "Fixture",
                    "marketCountry": "KR",
                    "quantity": "2",
                    "averagePurchasePrice": "68000",
                    "lastPrice": "70000",
                    "marketValue": {"amount": "140000"},
                }
            ],
        }

    async def get_sellable_quantity(self, symbol):
        return {"sellableQuantity": "1"}

    async def submit_order(self, **kwargs):
        assert self.loop is asyncio.get_running_loop()
        self.calls.append(kwargs)
        return {"submitted": True, "order_id": "fixture-order", "status": "SUBMITTED"}

    async def list_open_orders(self, symbol=None):
        return {
            "orders": [
                {
                    "orderId": "42",
                    "symbol": "005930",
                    "quantity": "2",
                    "execution": {"filledQuantity": "1"},
                    "status": "PARTIALLY_FILLED",
                }
            ]
        }

    async def cancel_order(self, order_id):
        self.calls.append(order_id)
        return {"status": "CANCELLED"}

    async def aclose(self):
        assert self.loop is asyncio.get_running_loop()
        self.closed = True


def test_toss_same_loop_reads_orders_and_close(live_safety, request_order):
    # 기본 sdk_dry_run 은 벤더 제출을 막는다. 이 테스트는 제출 번역만 본다.
    broker = Toss(
        account="fixture", account_seq=1, sdk_factory=LoopSDK, safety=live_safety, sdk_dry_run=False
    )
    for _ in range(3):
        assert broker.get_quote("005930").last == Decimal("70000")
    assert broker.get_balance().cash_available == Decimal("100000")
    assert broker.get_positions()[0].sellable == 1
    order = broker.place_order(request_order)
    assert order.id == "fixture-order"
    assert broker._sdk.calls[0]["side"] == "BUY"
    assert broker._sdk.calls[0]["quantity"] == "1"
    assert len(broker._sdk.calls[0]["client_order_id"]) <= 36
    assert (
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel")).status
        == "accepted"
    )
    broker.close()
    broker.close()
    assert broker._sdk.closed and broker._loop.is_closed()
    with pytest.raises(ConfigurationError):
        broker.get_quote("005930")


def test_toss_partial_cancel_not_silently_expanded(live_safety):
    broker = Toss(account="fixture", account_seq=1, sdk_factory=LoopSDK, safety=live_safety,
                  sdk_dry_run=False)
    with broker, pytest.raises(ConfigurationError, match="full remaining"):
        broker.cancel_order(CancelRequest(order_id="42", symbol="005930", quantity=2, client_key="cancel"))
    assert broker._sdk.calls == []


async def test_async_caller_gets_actionable_error():
    broker = Toss(account="fixture", account_seq=1, sdk_factory=LoopSDK)
    with pytest.raises(ConfigurationError, match="to_thread"):
        broker.get_quote("005930")
    assert broker._loop is None


def test_toss_rejects_mismatched_quote_symbol():
    class Wrong(LoopSDK):
        async def get_prices(self, symbols):
            return [{"symbol": "000660", "lastPrice": "70000"}]

    with Toss(account="fixture", account_seq=1, sdk_factory=Wrong) as broker:
        with pytest.raises(InvalidResponseError):
            broker.get_quote("005930")


def test_toss_kr_limit_sell_translation_unchanged(live_safety):
    # US 매도 개방(D11)이 KR 번역을 건드리지 않는지 본다.
    from pykbroker.models import OrderRequest

    broker = Toss(account="fixture", account_seq=1, sdk_factory=LoopSDK, safety=live_safety, sdk_dry_run=False)
    order = broker.place_order(
        OrderRequest(symbol="005930", side="sell", quantity=1, price="70000", client_key="kr-sell")
    )
    assert order.id == "fixture-order"
    assert len(broker._sdk.calls) == 1
    call = broker._sdk.calls[0]
    assert (call["symbol"], call["side"], call["order_type"], call["time_in_force"], call["price"]) == (
        "005930", "SELL", "LIMIT", "DAY", "70000"
    )
    broker.close()


def _mixed_sdk(rows):
    class MixedSDK(LoopSDK):
        async def list_open_orders(self, symbol=None):
            return {"orders": rows}

    return MixedSDK


_OURS = {
    "orderId": "ours-1",
    "symbol": "005930",
    "quantity": "3",
    "execution": {"filledQuantity": "1"},
    "status": "PARTIALLY_FILLED",
}
_FOREIGN_FRACTIONAL = {
    "orderId": "app-9",
    "symbol": "005930",
    "quantity": "0.5",
    "execution": {"filledQuantity": "0"},
    "status": "OPEN",
}


def _broker(live_safety, rows):
    return Toss(account="fixture", account_seq=1, sdk_factory=_mixed_sdk(rows), safety=live_safety,
                sdk_dry_run=False)


def test_toss_foreign_fractional_order_skipped_ours_readable_and_cancellable(live_safety, caplog):
    broker = _broker(live_safety, [_FOREIGN_FRACTIONAL, _OURS])
    with broker:
        assert [o.id for o in broker.list_orders("005930")] == ["ours-1"]
        found = broker.status(broker_order("ours-1"))
        assert (found.quantity, found.filled) == (3, 1)
        done = broker.cancel_order(CancelRequest(order_id="ours-1", symbol="005930", quantity=2, client_key="c"))
        assert done.status == "accepted"
        assert broker._sdk.calls == ["ours-1"]
    assert "0.5" not in caplog.text and "app-9" not in caplog.text


def test_toss_our_own_order_fractional_is_still_an_error(live_safety):
    ours_fractional = {**_OURS, "quantity": "2.5"}
    broker = _broker(live_safety, [_FOREIGN_FRACTIONAL, ours_fractional])
    with broker:
        with pytest.raises(InvalidResponseError):
            broker.status(broker_order("ours-1"))
        with pytest.raises(InvalidResponseError):
            broker.cancel_order(CancelRequest(order_id="ours-1", symbol="005930", quantity=2, client_key="c"))
        assert broker._sdk.calls == []


def test_toss_garbage_quantity_row_still_fails_closed(live_safety):
    broker = _broker(live_safety, [{**_FOREIGN_FRACTIONAL, "quantity": "abc"}, _OURS])
    with broker, pytest.raises(InvalidResponseError):
        broker.list_orders("005930")


def broker_order(order_id):
    from pykbroker import Order

    return Order(id=order_id, symbol="005930", market="KR")