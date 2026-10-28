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


def _row(symbol, quantity, country="US", **over):
    row = {"symbol": symbol, "name": "Fixture", "marketCountry": country,
           "currency": "USD" if country == "US" else "KRW", "quantity": quantity,
           "averagePurchasePrice": "100", "lastPrice": "100", "marketValue": {"amount": "0"}}
    row.update(over)
    return row


def _rows_sdk(rows, sellable):
    class SDK(LoopSDK):
        sellable_calls = []

        async def get_positions(self):
            return {"marketValue": {"amount": {"krw": "0", "usd": "0"}}, "items": [dict(r) for r in rows]}

        async def get_sellable_quantity(self, symbol):
            SDK.sellable_calls.append(symbol)
            return {"sellableQuantity": sellable[symbol]}

    return SDK


def _toss(SDK, live_safety, market="US"):
    kwargs = dict(allow_us_pilot=True) if market == "US" else {}
    return Toss(account="fixture", account_seq=1, sdk_factory=SDK, safety=live_safety, market=market,
                sdk_dry_run=False, **kwargs)


# 10-18 검수 527c P2-T: 한 응답에 같은 토스 종목이 두 줄이면 줄마다 종목 전체의 매도 가능 수량이 붙어 2주가 4주로 읽혔다.
# 토스 문서상 행 식별은 (symbol, marketCountry, currency)이고 매도 가능 수량은 심볼 하나로 읽는다(벤더 답이 기준).
@pytest.mark.parametrize("market,rows,answer,symbol,expected", [
    ("US", [_row("AAPL", "2"), _row("AAPL", "2")], "2", "AAPL", Decimal("2")),             # 같은 줄 반복 → 한 번
    ("US", [_row("AAPL", "2.57"), _row("AAPL", "2.57")], "2.57", "AAPL", Decimal("2.57")),  # 소수 반복
    ("KR", [_row("005930", "2", country="KR"), _row("005930", "2", country="KR")], "2", "005930", Decimal("2")),
    ("US", [_row("AAPL", "2"), _row("AAPL", "2", currency="KRW")], "2", "AAPL", Decimal("2")),  # 다른 통화 줄: 벤더 답 한 번
    ("US", [_row("AAPL", "2"), _row("MSFT", "4"), _row("005930", "7", country="KR")], "2", "AAPL", Decimal("2")),
    ("US", [_row("MSFT", "4")], "0", "AAPL", Decimal("0")),                                 # 미보유 → 0
])
def test_toss_sellable_quantity_reads_the_vendor_answer_once(live_safety, market, rows, answer, symbol, expected):
    SDK = _rows_sdk(rows, {symbol: answer})
    with _toss(SDK, live_safety, market) as broker:
        held = broker.sellable_quantity(symbol)
    assert held == expected and isinstance(held, Decimal)
    assert SDK.sellable_calls == ([symbol] if expected else [])


def test_toss_repeated_row_is_one_position_with_the_vendor_sellable(live_safety):
    SDK = _rows_sdk([_row("AAPL", "2"), _row("AAPL", "2"), _row("MSFT", "2.57")], {"AAPL": "2", "MSFT": "2.57"})
    with _toss(SDK, live_safety) as broker:
        positions = broker.get_positions()
    assert [(p.symbol, p.sellable_exact) for p in positions] == [("AAPL", Decimal("2")), ("MSFT", Decimal("2.57"))]
    assert sorted(SDK.sellable_calls) == ["AAPL", "MSFT"]


def test_toss_distinct_currency_rows_share_one_sellable_answer(live_safety):
    # 같은 종목·다른 통화(식별이 다른 두 줄)는 따로 두되, 매도 가능 수량은 벤더 답 하나를 나눠 갖는다(합 = 답).
    SDK = _rows_sdk([_row("AAPL", "2"), _row("AAPL", "2", currency="KRW")], {"AAPL": "3"})
    with _toss(SDK, live_safety) as broker:
        positions = broker.get_positions()
    assert [p.sellable_exact for p in positions] == [Decimal("2"), Decimal("1")]
    assert sum(p.sellable_exact for p in positions) == Decimal("3")
    assert SDK.sellable_calls == ["AAPL"]


@pytest.mark.parametrize("market,rows,answer,expected", [
    # 같은 식별·값만 다른 줄(수량·시세·평단): 줄을 어떻게 읽어도 벤더 답 한 번으로 판정한다(검수 535 P1).
    ("US", [_row("AAPL", "2"), _row("AAPL", "5")], "2", Decimal("2")),
    ("US", [_row("AAPL", "2"), _row("AAPL", "2", lastPrice="101")], "2", Decimal("2")),
    ("US", [_row("AAPL", "2"), _row("AAPL", "2", averagePurchasePrice="150.00")], "4", Decimal("4")),
    ("US", [_row("AAPL", "2.57"), _row("AAPL", "2.5")], "2.57", Decimal("2.57")),
    ("KR", [_row("005930", "2", country="KR"), _row("005930", "3", country="KR")], "2", Decimal("2")),
])
def test_toss_rows_differing_only_in_values_use_the_vendor_answer(live_safety, market, rows, answer, expected):
    symbol = rows[-1]["symbol"]
    SDK = _rows_sdk(rows, {symbol: answer})
    with _toss(SDK, live_safety, market) as broker:
        assert broker.sellable_quantity(symbol) == expected
        positions = broker.get_positions()
    assert sum(p.sellable_exact if p.sellable_exact is not None else Decimal(p.sellable) for p in positions) == expected
    assert SDK.sellable_calls == [symbol, symbol]  # 각 호출이 벤더 답을 한 번씩만


@pytest.mark.parametrize("market,rows", [
    ("US", [_row("AAPL", "2", country="KR"), _row("AAPL", "2")]),      # 두 시장의 같은 심볼(답을 나눌 수 없음)
])
def test_toss_contradicting_rows_are_unknown(live_safety, market, rows):
    symbol = rows[-1]["symbol"]
    SDK = _rows_sdk(rows, {symbol: "2"})
    with _toss(SDK, live_safety, market) as broker:
        with pytest.raises(InvalidResponseError):
            broker.sellable_quantity(symbol)
        with pytest.raises(InvalidResponseError):
            broker.get_positions()
    assert SDK.sellable_calls == []  # 모순 응답에서는 매도 가능 수량을 읽기 전에 멈춘다


@pytest.mark.parametrize("rows,answer", [
    ([_row("AAPL", "2"), _row("AAPL", "2")], "4"),                       # 같은 줄 2+2 에 벤더 답 4: 한 보유를 넘음
    ([_row("AAPL", "2"), _row("AAPL", "5")], "7.5"),                     # 값이 달라도 답이 줄 합을 넘으면 모름
])
def test_toss_vendor_answer_above_the_rows_is_unknown(live_safety, rows, answer):
    SDK = _rows_sdk(rows, {"AAPL": answer})
    with _toss(SDK, live_safety) as broker, pytest.raises(InvalidResponseError):
        broker.sellable_quantity("AAPL")


def test_toss_sellable_answer_above_the_holding_is_unknown(live_safety):
    SDK = _rows_sdk([_row("AAPL", "2"), _row("AAPL", "2")], {"AAPL": "2.1"})
    with _toss(SDK, live_safety) as broker, pytest.raises(InvalidResponseError):
        broker.sellable_quantity("AAPL")


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
    balance = broker.get_balance()
    assert balance.cash_available == balance.orderable_cash == Decimal("100000")
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
