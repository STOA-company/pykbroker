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
