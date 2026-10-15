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


class FractionalSDK(LoopSDK):
    async def get_positions(self):
        return {
            "marketValue": {"amount": {"krw": "0", "usd": "30"}},
            "items": [
                {"symbol": "NKE", "name": "Fixture", "marketCountry": "US", "quantity": "0.3",
                 "averagePurchasePrice": "100", "lastPrice": "100", "marketValue": {"amount": "30"}},
                {"symbol": "AAPL", "name": "Fixture", "marketCountry": "US", "quantity": "2.5",
                 "averagePurchasePrice": "100", "lastPrice": "100", "marketValue": {"amount": "250"}},
                {"symbol": "MSFT", "name": "Fixture", "marketCountry": "US", "quantity": "3",
                 "averagePurchasePrice": "100", "lastPrice": "100", "marketValue": {"amount": "300"}},
            ],
        }

    async def get_sellable_quantity(self, symbol):
        return {"sellableQuantity": {"NKE": "0.3", "AAPL": "2.1", "MSFT": "1"}[symbol]}


def test_toss_us_fractional_holding_is_read_not_500(live_safety):
    # 미국 소수점 보유 한 줄이 잔고 조회 전체를 500 으로 만들던 버그. 소수는 읽고 정수 칸은 내림한다.
    broker = Toss(account="fixture", account_seq=1, sdk_factory=FractionalSDK, safety=live_safety,
                  market="US", allow_us_pilot=True, sdk_dry_run=False)
    by = {p.symbol: p for p in broker.get_positions()}
    assert by["NKE"].quantity == 0 and by["NKE"].sellable == 0
    assert by["NKE"].quantity_exact == Decimal("0.3") and by["NKE"].sellable_exact == Decimal("0.3")
    assert by["AAPL"].quantity == 2 and by["AAPL"].sellable == 2
    assert by["AAPL"].quantity_exact == Decimal("2.5") and by["AAPL"].sellable_exact == Decimal("2.1")
    assert by["MSFT"].quantity == 3 and by["MSFT"].sellable == 1
    assert by["MSFT"].quantity_exact == Decimal("3") and by["MSFT"].sellable_exact == Decimal("1")
    for pos in by.values():
        assert type(pos.quantity) is int and type(pos.sellable) is int
        assert isinstance(pos.quantity_exact, Decimal) and isinstance(pos.sellable_exact, Decimal)
    assert by["AAPL"].model_dump(mode="json")["sellable_exact"] == "2.1"
    broker.close()


class FractionalKRSDK(FractionalSDK):
    quantity = "0.5"
    sellable_quantity = "0"

    async def get_positions(self):
        return {
            "marketValue": {"amount": {"krw": "1", "usd": "0"}},
            "items": [
                {"symbol": "005930", "name": "Fixture", "marketCountry": "KR", "quantity": self.quantity,
                 "averagePurchasePrice": "100", "lastPrice": "100", "marketValue": {"amount": "50"}},
            ],
        }

    async def get_sellable_quantity(self, symbol):
        return {"sellableQuantity": self.sellable_quantity}


@pytest.mark.parametrize("quantity,sellable", [("0.5", "0"), ("2", "1.5")])
def test_toss_kr_fractional_quantity_stays_invalid_response(live_safety, quantity, sellable):
    # 국내(KR)는 보유·매도가능 수량 모두 정수다. 두 칸 중 하나라도 소수면 계속 오류로 낸다.
    class SDK(FractionalKRSDK):
        pass

    SDK.quantity, SDK.sellable_quantity = quantity, sellable
    with Toss(account="fixture", account_seq=1, sdk_factory=SDK, safety=live_safety,
              market="KR", sdk_dry_run=False) as broker, pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_toss_us_sellable_above_exact_holding_stays_invalid_response(live_safety):
    class SDK(FractionalSDK):
        async def get_sellable_quantity(self, symbol):
            return {"sellableQuantity": "2.6" if symbol == "AAPL" else "0"}

    # 2.5주 보유·2.6주 매도가능은 둘 다 내리면 2지만, 정확한 수량의 모순은 거부해야 한다.
    with Toss(account="fixture", account_seq=1, sdk_factory=SDK, safety=live_safety,
              market="US", allow_us_pilot=True, sdk_dry_run=False) as broker, pytest.raises(InvalidResponseError):
        broker.get_positions()


def test_toss_kr_whole_quantity_has_no_exact(live_safety):
    broker = Toss(account="fixture", account_seq=1, sdk_factory=LoopSDK, safety=live_safety, sdk_dry_run=False)
    pos = broker.get_positions()[0]
    assert (pos.quantity, pos.sellable) == (2, 1)
    assert pos.quantity_exact is None and pos.sellable_exact is None
    broker.close()


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
