"""US pilot contract with an injected SDK; no network or live order."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pykbroker import Toss
from pykbroker.exceptions import (
    DryRunError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TossRejectedError,
    TransportError,
)
from pykbroker.models import CancelRequest, OrderRequest


class _SDK:
    def __init__(self, **options):
        self.options = options
        self.calls = []

    async def get_positions(self):
        return {"marketValue": {"amount": {"usd": "3.25", "krw": "0"}}, "items": []}

    async def get_prices(self, symbols):
        self.calls.append(("get_prices", tuple(symbols)))
        return [{"symbol": symbols[0], "currency": "USD", "lastPrice": "13.06"}]

    async def get_orderbook(self, symbol):
        self.calls.append(("get_orderbook", symbol))
        return {"currency": "USD", "asks": [{"price": "13.07"}], "bids": [{"price": "13.05"}]}

    async def get_buying_power(self, currency):
        self.calls.append(("buying_power", currency))
        return {"currency": currency, "cashBuyingPower": "0" if currency == "USD" else "30000"}

    async def get_exchange_rate(self, base, quote):
        self.calls.append(("exchange_rate", base, quote))
        return {"baseCurrency": base, "quoteCurrency": quote, "rate": "1350"}

    async def list_open_orders(self, symbol=None):
        self.calls.append(("list_orders", symbol))
        return {"hasNext": False, "orders": [{"orderId": "o-1", "symbol": "F", "quantity": "1",
                                             "execution": {"filledQuantity": "0"}, "status": "OPEN"}]}

    async def submit_order(self, **kwargs):
        self.calls.append(("submit_order", kwargs))
        return {"submitted": True, "order_id": "o-1"}

    async def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return {"status": "CANCELLED"}


def _broker(**kwargs):
    made = {}

    def factory(**options):
        made["sdk"] = _SDK(**options)
        return made["sdk"]

    broker = Toss(account="fixture", account_seq=7, market="US", allow_us_pilot=True,
                  sdk_factory=factory, **kwargs)
    return broker, made


def _order(**kwargs):
    options = dict(symbol="F", side="buy", quantity=1, price="13.12", market="US", client_key="pilot-1")
    options.update(kwargs)
    return OrderRequest(**options)


def test_us_remains_locked_without_explicit_constructor_option():
    with pytest.raises(SpecUnverifiedError):
        Toss(account="fixture", account_seq=7, market="US", sdk_factory=_SDK)
    with pytest.raises(NotSupportedError):
        Toss(account="fixture", account_seq=7, sdk_factory=_SDK).funding_snapshot()


def test_removing_pilot_option_relocks_us_construction():
    broker, _ = _broker()
    assert broker.capabilities().markets == ("US",)
    with pytest.raises(SpecUnverifiedError):
        Toss(account="fixture", account_seq=7, market="US", allow_us_pilot=False, sdk_factory=_SDK)


def test_us_read_contract_and_currency_guards():
    broker, made = _broker()
    assert broker.capabilities().markets == ("US",)
    assert broker.capabilities().requires_venue is False
    assert broker.get_balance().currency == "USD"
    quote = broker.get_quote("F", market="US")
    assert (quote.last, quote.bid, quote.ask) == (Decimal("13.06"), Decimal("13.05"), Decimal("13.07"))
    assert broker.funding_snapshot() == {
        "usd_buying_power": Decimal("0"), "krw_buying_power": Decimal("30000"),
        "usd_krw_rate": Decimal("1350"),
    }
    assert ("buying_power", "USD") in made["sdk"].calls
    assert ("exchange_rate", "USD", "KRW") in made["sdk"].calls
    assert ("get_orderbook", "F") in made["sdk"].calls
    assert broker.list_orders("F")[0].status == "open"


def test_us_funding_rejects_wrong_fx_pair():
    broker, _ = _broker()
    class Wrong(_SDK):
        async def get_exchange_rate(self, base, quote):
            return {"baseCurrency": "KRW", "quoteCurrency": "USD", "rate": "1350"}
    broker._factory = Wrong
    with pytest.raises(InvalidResponseError):
        broker.funding_snapshot()


def test_us_quote_rejects_wrong_book_currency_and_keeps_empty_side_missing():
    broker, _ = _broker()

    class Wrong(_SDK):
        async def get_orderbook(self, symbol):
            return {"currency": "KRW", "asks": [{"price": "13.07"}], "bids": [{"price": "13.05"}]}

    broker._factory = Wrong
    with pytest.raises(InvalidResponseError):
        broker.get_quote("F", market="US")

    empty, _ = _broker()

    class Empty(_SDK):
        async def get_orderbook(self, symbol):
            return {"currency": "USD", "asks": [], "bids": [{"price": "13.05"}]}

    empty._factory = Empty
    assert empty.get_quote("F", market="US").ask is None


def test_us_empty_holdings_null_value_is_zero():
    broker, _ = _broker()

    class Empty(_SDK):
        async def get_positions(self):
            return {"marketValue": {"amount": {"usd": None, "krw": None}}, "items": []}

    broker._factory = Empty
    assert broker.get_balance().position_value == Decimal("0")


def test_us_limit_buy_day_order_and_cancel_once_each():
    broker, made = _broker(sdk_dry_run=False)
    result = broker._place_order(_order())
    assert result.id == "o-1"
    submitted = [call for call in made["sdk"].calls if call[0] == "submit_order"]
    assert len(submitted) == 1
    assert submitted[0][1]["quantity"] == "1"
    assert submitted[0][1]["side"] == "BUY"
    assert submitted[0][1]["order_type"] == "LIMIT"
    assert submitted[0][1]["time_in_force"] == "DAY"
    assert submitted[0][1]["price"] == "13.12"
    assert broker._cancel_order(CancelRequest(order_id="o-1", symbol="F", quantity=1,
                                               market="US", client_key="cancel-1")).id == "o-1"
    assert [call for call in made["sdk"].calls if call[0] == "cancel_order"] == [("cancel_order", "o-1")]


def test_us_sell_market_and_kr_symbol_never_submit():
    broker, made = _broker(sdk_dry_run=False)
    for request in (
        _order(side="sell"),
        OrderRequest(symbol="F", side="buy", quantity=1, order_type="market", market="US", client_key="market"),
        OrderRequest(symbol="005930", side="buy", quantity=1, price="13.12", market="US", client_key="kr"),
    ):
        with pytest.raises(NotSupportedError):
            broker._place_order(request)
    assert "sdk" not in made


def test_us_dry_run_blocks_submit_and_cancel():
    broker, made = _broker()
    with pytest.raises(DryRunError):
        broker._place_order(_order())
    with pytest.raises(DryRunError):
        broker._cancel_order(CancelRequest(order_id="o-1", symbol="F", quantity=1,
                                                   market="US", client_key="cancel-1"))
    assert "sdk" not in made


@pytest.mark.parametrize(
    ("status", "code", "error_type"),
    [(400, "E123", TossRejectedError), (408, "request-timeout", TransportError),
     (409, "request-in-progress", TransportError),
     (409, "idempotency-key-conflict", TransportError),
     (422, "idempotency-key-conflict", TransportError),
     (401, "token-revoked", TossRejectedError), (500, "internal-error", TransportError)],
)
def test_one_post_and_correct_outcome_for_vendor_error(status, code, error_type):
    pytest.importorskip("toss_openapi")
    from toss_openapi.errors import TossApiError
    from toss_openapi.models import order as order_models

    from pykbroker.brokers.toss import _TossBridge

    class RejectingTransport:
        calls = 0

        async def request(self, method, path, **kwargs):
            self.calls += 1
            assert (method, path, kwargs["retryable"]) == ("POST", "/api/v1/orders", False)
            assert kwargs["json_body"]["timeInForce"] == "DAY"
            raise TossApiError("private vendor detail", status_code=status, code=code)

    class TokenManager:
        calls = 0

        async def get_access_token(self):
            self.calls += 1
            return "fixture-token"

    transport = RejectingTransport()
    manager = TokenManager()
    bridge = object.__new__(_TossBridge)
    bridge._dry_run = False
    bridge._models = order_models
    bridge._api_error_type = TossApiError
    bridge._guard = SimpleNamespace(check_create=lambda request, account: None)
    bridge._context = object()
    bridge._manager = manager
    bridge._transport = transport
    bridge._account_seq = 7
    with pytest.raises(error_type) as captured:
        asyncio.run(bridge.submit_order(symbol="F", side="BUY", order_type="LIMIT", quantity="1",
                                        price="13.12", time_in_force="DAY", client_order_id="pilot-1"))
    if error_type is TossRejectedError:
        assert captured.value.code == code
    assert "private vendor detail" not in str(captured.value)
    assert transport.calls == 1
    assert manager.calls == 1


def test_sdk_bridge_success_posts_once_and_cancel_is_only_acknowledged():
    pytest.importorskip("toss_openapi")
    from toss_openapi.errors import TossApiError
    from toss_openapi.models import order as order_models

    from pykbroker.brokers.toss import _TossBridge

    class Transport:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"orderId": "cancel-new" if path.endswith("/cancel") else "buy-1"}

    class Manager:
        async def get_access_token(self):
            return "fixture-token"

    transport = Transport()
    bridge = object.__new__(_TossBridge)
    bridge._dry_run = False
    bridge._models = order_models
    bridge._api_error_type = TossApiError
    bridge._guard = SimpleNamespace(check_create=lambda request, account: None,
                                    check_live_allowed=lambda account: None)
    bridge._context = object()
    bridge._manager = Manager()
    bridge._transport = transport
    bridge._account_seq = 7
    submitted = asyncio.run(bridge.submit_order(symbol="F", side="BUY", order_type="LIMIT",
                                                quantity="1", price="13.12", time_in_force="DAY",
                                                client_order_id="pilot-1"))
    acknowledged = asyncio.run(bridge.cancel_order("buy-1"))
    assert submitted["order_id"] == "buy-1"
    assert acknowledged == {"submitted": True, "status": "CANCEL_SUBMITTED", "order_id": "cancel-new"}
    assert [path for method, path, _ in transport.calls] == ["/api/v1/orders", "/api/v1/orders/buy-1/cancel"]
    assert all(kwargs["retryable"] is False and kwargs["account_seq"] == 7
               for _, _, kwargs in transport.calls)
