"""Toss US fractional shares (OpenAPI 1.2.24): amount MARKET buy / decimal MARKET sell.

The SDK bridge is the contract mock (``toss_contract_mock``) built from the vendor schema; no network,
no live order. Whole-share behaviour (US LIMIT only, KR integers) stays byte-for-byte as before.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from toss_contract_mock import ContractMockTossSdk

from pykbroker import DBFI, KIS, Toss
from pykbroker.brokers import toss as toss_module
from pykbroker.brokers.toss import fractional_market_allowed, in_fractional_session
from pykbroker.exceptions import (
    FractionalDisabledError,
    FractionalNotSupportedError,
    FractionalOutsideSessionError,
    NotSupportedError,
    TossRejectedError,
)
from pykbroker.models import OrderRequest
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
IN_SESSION = datetime(2026, 10, 16, 10, 0, tzinfo=ET)          # Friday 10:00 ET
AFTER_CUTOFF = datetime(2026, 10, 16, 15, 0, tzinfo=ET)        # 15:00 ET: one hour before close
PRE_OPEN = datetime(2026, 10, 16, 9, 29, 59, tzinfo=ET)
WEEKEND = datetime(2026, 10, 17, 11, 0, tzinfo=ET)
USD_KRW = Decimal("1381.2")
DISABLED_SENTENCE = "소수점 주문은 아직 켜지지 않았어요"  # 소수점 주문은 아직 켜지지 않았어요


def _valuation(request):
    """Conservative KRW estimate: amount buys value at the USD amount, decimal sells at the last price."""
    usd = request.amount if request.amount is not None else Decimal(request.quantity) * Decimal("190.00")
    return usd * USD_KRW


@pytest.fixture
def live_safety():
    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="10000000", daily_krw_cap="20000000"),
        risk_ledger=MemoryRiskLedger(), idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda broker, account: False),
        valuation=_valuation, market_order_allowed=lambda request: request.fractional,
    )


def _broker(clock=lambda: IN_SESSION, **kwargs):
    made = {}

    def factory(**options):
        made["sdk"] = ContractMockTossSdk(clock=clock, **options)
        return made["sdk"]

    broker = Toss(account="fixture", account_seq=7, market="US", allow_us_pilot=True,
                  sdk_factory=factory, sdk_dry_run=False, sdk_live_trading_enabled=True, clock=clock, **kwargs)
    return broker, made


def _submits(made):
    sdk = made.get("sdk")
    return [] if sdk is None else [payload for name, payload in sdk.calls if name == "submit_order"]


# --- model: exactly one of quantity/amount, fractional only where the vendor allows ----------- #

def test_amount_buy_model_is_us_market_buy_only_two_decimals():
    order = OrderRequest(symbol="AAPL", side="buy", amount="57.12", market="US", order_type="market", client_key="k")
    assert order.fractional is True and order.quantity is None and order.amount == Decimal("57.12")
    for bad in (dict(side="sell"), dict(order_type="limit", price="190"), dict(market="KR")):
        with pytest.raises(ValidationError):
            OrderRequest(**{**dict(symbol="AAPL", side="buy", amount="57.12", market="US", order_type="market",
                                   client_key="k"), **bad})
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side="buy", amount="57.123", market="US", order_type="market", client_key="k")
    with pytest.raises(ValidationError):  # both quantity and amount
        OrderRequest(symbol="AAPL", side="buy", quantity=1, amount="57.12", market="US", order_type="market",
                     client_key="k")
    with pytest.raises(ValidationError):  # neither
        OrderRequest(symbol="AAPL", side="buy", market="US", order_type="market", client_key="k")


def test_fractional_sell_model_is_us_market_sell_only_six_decimals():
    order = OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("0.3"), market="US", order_type="market",
                         client_key="k")
    assert order.fractional is True and order.quantity == Decimal("0.3")
    whole = OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("3"), market="US", order_type="limit",
                         price="190", client_key="k")
    assert whole.fractional is False and whole.quantity == 3 and type(whole.quantity) is int
    for bad in (dict(side="buy"), dict(order_type="limit", price="190"), dict(market="KR")):
        with pytest.raises(ValidationError):
            OrderRequest(**{**dict(symbol="AAPL", side="sell", quantity=Decimal("0.3"), market="US",
                                   order_type="market", client_key="k"), **bad})
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("0.1234567"), market="US", order_type="market",
                     client_key="k")
    with pytest.raises(ValidationError):  # floats are never accepted as a share quantity
        OrderRequest(symbol="AAPL", side="sell", quantity=0.3, market="US", order_type="market", client_key="k")


# --- Toss US: the two fractional paths reach the vendor contract verbatim ---------------------- #

def test_amount_market_buy_sends_order_amount_without_quantity_price_or_tif(live_safety):
    broker, made = _broker(safety=live_safety)
    order = broker.place(OrderRequest(symbol="AAPL", side="buy", amount="57.12", market="US", order_type="market",
                                      client_key="amt-1"))
    [payload] = _submits(made)
    assert payload["orderAmount"] == "57.12" and payload["orderType"] == "MARKET" and payload["side"] == "BUY"
    assert "quantity" not in payload and "price" not in payload and "timeInForce" not in payload
    assert payload["clientOrderId"]
    assert order.id == "contract-order-1" and order.amount == Decimal("57.12") and order.quantity is None
    assert order.status == "accepted"


def test_fractional_market_sell_sends_decimal_quantity_verbatim(live_safety):
    broker, made = _broker(safety=live_safety)
    order = broker.place(OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("0.300001"), market="US",
                                      order_type="market", client_key="frac-1"))
    [payload] = _submits(made)
    assert payload["quantity"] == "0.300001" and payload["orderType"] == "MARKET" and payload["side"] == "SELL"
    assert "price" not in payload and payload["timeInForce"] == "DAY" and "orderAmount" not in payload
    assert order.quantity_exact == Decimal("0.300001") and order.quantity == 0  # floor, never rounded up


def test_whole_share_us_market_stays_limit_only_and_unchanged(live_safety):
    broker, made = _broker(safety=live_safety)
    with pytest.raises(NotSupportedError, match="not supported"):
        broker.place(OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("3"), market="US",
                                  order_type="market", client_key="whole-1"))
    assert _submits(made) == []
    broker.place(OrderRequest(symbol="AAPL", side="buy", quantity=2, price="190.00", market="US",
                              order_type="limit", client_key="limit-1"))
    [payload] = _submits(made)
    assert payload["quantity"] == "2" and payload["price"] == "190.00" and payload["orderType"] == "LIMIT"


# --- refusals before any HTTP ----------------------------------------------------------------- #

@pytest.mark.parametrize("clock", [lambda: AFTER_CUTOFF, lambda: PRE_OPEN, lambda: WEEKEND])
def test_outside_us_regular_session_is_refused_before_http(live_safety, clock):
    broker, made = _broker(clock=clock, safety=live_safety)
    with pytest.raises(FractionalOutsideSessionError) as info:
        broker.place(OrderRequest(symbol="AAPL", side="buy", amount="10.00", market="US", order_type="market",
                                  client_key="late-1"))
    assert info.value.code == "FRACTIONAL_OUTSIDE_SESSION"
    assert _submits(made) == []


def test_session_window_is_et_0930_to_1500_weekdays():
    assert in_fractional_session(IN_SESSION) is True
    assert in_fractional_session(datetime(2026, 10, 16, 9, 30, tzinfo=ET)) is True
    assert in_fractional_session(AFTER_CUTOFF) is False
    assert in_fractional_session(PRE_OPEN) is False
    assert in_fractional_session(WEEKEND) is False
    # 23:30 KST on a Friday is 10:30 ET the same Friday (EDT): inside.
    assert in_fractional_session(datetime(2026, 10, 16, 23, 30, tzinfo=KST)) is True
    with pytest.raises(Exception):
        in_fractional_session(datetime(2026, 10, 16, 10, 0))  # naive clock refused


def test_flag_off_refuses_with_the_customer_sentence(live_safety, monkeypatch):
    monkeypatch.setenv("FRACTIONAL_MARKET_ALLOWED", "0")
    assert fractional_market_allowed() is False
    broker, made = _broker(safety=live_safety)
    with pytest.raises(FractionalDisabledError) as info:
        broker.place(OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("0.5"), market="US",
                                  order_type="market", client_key="off-1"))
    assert info.value.code == "FRACTIONAL_DISABLED"
    assert str(info.value) == DISABLED_SENTENCE
    assert _submits(made) == []
    # Whole-share LIMIT keeps working with the flag off.
    broker.place(OrderRequest(symbol="AAPL", side="buy", quantity=1, price="190.00", market="US",
                              order_type="limit", client_key="limit-off"))
    assert len(_submits(made)) == 1


@pytest.mark.parametrize("raw,expected", [(None, True), ("1", True), ("true", True), ("ON", True),
                                          ("0", False), ("false", False), ("off", False), ("No", False)])
def test_flag_default_on_only_explicit_off_values_disable(monkeypatch, raw, expected):
    monkeypatch.delenv("FRACTIONAL_MARKET_ALLOWED", raising=False)
    if raw is not None:
        monkeypatch.setenv("FRACTIONAL_MARKET_ALLOWED", raw)
    assert fractional_market_allowed() is expected
    assert fractional_market_allowed({} if raw is None else {"FRACTIONAL_MARKET_ALLOWED": raw}) is expected


def test_kis_and_dbfi_refuse_fractional_explicitly_without_int(live_safety):
    import inspect

    from pykbroker.brokers import dbfi as dbfi_module
    from pykbroker.brokers import kis as kis_module

    brokers = [
        KIS(account="fixture-account", client=Mock(), api=Mock(), safety=live_safety, market="US", venue="NASDAQ"),
        DBFI(account="fixture-account", client=Mock(), api=Mock(), safety=live_safety, market="US", venue="NASDAQ"),
    ]
    sell = OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("0.5"), market="US", order_type="market",
                        venue="NASDAQ", client_key="kis-frac")
    buy = OrderRequest(symbol="AAPL", side="buy", amount="20.00", market="US", order_type="market",
                       venue="NASDAQ", client_key="kis-amt")
    for broker in brokers:
        for request in (sell, buy):
            with pytest.raises(FractionalNotSupportedError) as info:
                broker.place(request)
            assert info.value.code == "FRACTIONAL_NOT_SUPPORTED"
        assert broker.capabilities().supports_amount_market_buy is False
        assert broker.capabilities().supports_fractional_market_sell is False
    for module in (kis_module, dbfi_module):
        src = inspect.getsource(module)
        assert "int(request.quantity)" not in src and "int(quantity)" not in src
        assert "FractionalNotSupportedError" in src


def test_toss_kr_never_takes_fractional(live_safety):
    broker = Toss(account="fixture", account_seq=7, sdk_factory=ContractMockTossSdk, sdk_dry_run=False,
                  sdk_live_trading_enabled=True, safety=live_safety)
    assert broker.capabilities().supports_amount_market_buy is False
    assert broker.capabilities().supports_fractional_market_sell is False
    with pytest.raises(ValidationError):
        OrderRequest(symbol="005930", side="sell", quantity=Decimal("0.5"), market="KR", order_type="market",
                     client_key="kr-frac")


# --- the contract mock itself answers like the vendor ----------------------------------------- #

@pytest.mark.asyncio
async def test_contract_mock_rejects_what_the_vendor_rejects():
    sdk = ContractMockTossSdk(clock=lambda: IN_SESSION)
    base = dict(symbol="AAPL", side="SELL", order_type="MARKET", client_order_id="c1")
    with pytest.raises(TossRejectedError) as info:
        await sdk.submit_order(**{**base, "order_type": "LIMIT"}, quantity="0.5", price="190")
    assert info.value.code == "invalid-request"
    with pytest.raises(TossRejectedError):
        await sdk.submit_order(**base, quantity="0.1234567")           # scale > 6
    with pytest.raises(TossRejectedError):
        await sdk.submit_order(**{**base, "side": "BUY"}, quantity="0.5")  # fractional buy
    with pytest.raises(TossRejectedError):
        await sdk.submit_order(symbol="AAPL", side="BUY", order_type="LIMIT", order_amount="10.00",
                               client_order_id="c2")                   # amount + LIMIT
    with pytest.raises(TossRejectedError):
        await sdk.submit_order(**base, quantity="1", order_amount="10.00")  # both
    late = ContractMockTossSdk(clock=lambda: AFTER_CUTOFF)
    with pytest.raises(TossRejectedError) as info:
        await late.submit_order(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="10.00",
                                client_order_id="c3")
    assert info.value.code == "amount-order-outside-regular-hours"
    ok = await sdk.submit_order(**base, quantity="0.5")
    assert ok["submitted"] is True and sdk.accepted[-1]["quantity"] == "0.5"


def test_toss_module_still_uses_str_for_whole_quantities():
    import inspect

    src = inspect.getsource(toss_module)
    assert "format(request.quantity, \"f\") if isinstance(request.quantity, Decimal)" in src
    assert "int(request.quantity)" not in src
