"""US SELL on Toss / KIS / DB (2026-10-16, platform 16 «ussell»): what each adapter really sends.

- Toss: the REAL SDK bridge + vendor models against the OpenAPI 1.2.24 contract transport (no network).
  Whole-share US SELL is a LIMIT body (D11); «sell all» of a fractional holding takes the exact
  ``sellable_exact`` from ``get_positions`` and sends it verbatim as a MARKET SELL (never floored).
- KIS / DB: whole-share US SELL LIMIT reaches the vendor client with the side, quantity, price and the
  symbol's exchange (NASD/NYSE/AMEX). Fractional or MARKET US sells are refused before any vendor call.
Nothing here places a real order or opens a socket.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from tests.toss_contract_mock import ContractTransport

from pykbroker import DBFI, KIS, Toss
from pykbroker.exceptions import FractionalNotSupportedError, NotSupportedError
from pykbroker.models import OrderRequest, Position
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

ET = ZoneInfo("America/New_York")
IN_SESSION = datetime(2026, 10, 16, 10, 0, tzinfo=ET)  # Friday 10:00 ET
HOLDING = "1.234567"
# The exact-holding read (quantity_exact/sellable_exact) is #36: on the serving line (54451a9d), not yet on main.
needs_exact_holdings = pytest.mark.skipif("sellable_exact" not in Position.model_fields,
                                          reason="Position.sellable_exact arrives with pykbroker #36")


def _valuation(request):
    qty = request.quantity if request.quantity is not None else Decimal("0")
    usd = request.amount if request.amount is not None else Decimal(qty) * Decimal("190.00")
    return usd * Decimal("1381.2")


@pytest.fixture
def live_safety():
    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="10000000", daily_krw_cap="20000000"),
        risk_ledger=MemoryRiskLedger(), idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda broker, account: False),
        valuation=_valuation, market_order_allowed=lambda request: request.fractional,
    )


@pytest.fixture
def live_sdk_env(monkeypatch):
    monkeypatch.setenv("TOSS_LIVE_TRADING_ENABLED", "true")
    monkeypatch.delenv("FRACTIONAL_MARKET_ALLOWED", raising=False)


def _toss(transport, safety, holding=HOLDING):
    pytest.importorskip("toss_openapi")
    from tests.test_toss_us_fractional import _real_bridge

    base, _ = _real_bridge(transport)

    class Bridge(base):
        async def get_positions(self):
            return {"marketValue": {"amount": {"usd": "234.57", "krw": "0"}}, "items": [{
                "symbol": "AAPL", "name": "fixture", "marketCountry": "US", "quantity": holding,
                "averagePurchasePrice": "180.00", "lastPrice": "190.00",
                "marketValue": {"amount": "234.57"}}]}

        async def get_sellable_quantity(self, symbol):
            assert symbol == "AAPL"
            return {"symbol": symbol, "sellableQuantity": holding}

    return Toss(account="fixture", account_seq=7, market="US", allow_us_pilot=True, sdk_factory=Bridge,
                sdk_dry_run=False, sdk_live_trading_enabled=True, clock=lambda: IN_SESSION, safety=safety)


def test_toss_whole_share_us_sell_limit_body_matches_the_vendor_schema(live_safety, live_sdk_env):
    transport = ContractTransport(clock=lambda: IN_SESSION)
    order = _toss(transport, live_safety).place(OrderRequest(
        symbol="AAPL", side="sell", quantity=3, price="190.05", market="US", order_type="limit",
        client_key="us-sell-limit-1"))
    [(method, path, body, retryable, seq)] = transport.calls
    assert (method, path, retryable, seq) == ("POST", "/api/v1/orders", False, 7)
    assert body["side"] == "SELL" and body["orderType"] == "LIMIT" and body["timeInForce"] == "DAY"
    assert body["quantity"] == "3" and body["price"] == "190.05" and body["symbol"] == "AAPL"
    assert "orderAmount" not in body
    assert order.side == "sell" and order.quantity == 3 and order.status == "accepted"


@needs_exact_holdings
def test_toss_sell_all_of_a_fractional_holding_sends_the_exact_quantity(live_safety, live_sdk_env):
    transport = ContractTransport(clock=lambda: IN_SESSION)
    broker = _toss(transport, live_safety)
    [position] = broker.get_positions()
    # Whole-share fields stay floored for integer consumers; the exact holding is the *_exact pair.
    assert (position.quantity, position.sellable) == (1, 1)
    assert position.quantity_exact == position.sellable_exact == Decimal(HOLDING)
    assert position.model_dump(mode="json")["sellable_exact"] == HOLDING  # what the T balance route serialises
    order = broker.place(OrderRequest(symbol="AAPL", side="sell", quantity=position.sellable_exact, market="US",
                                      order_type="market", client_key="us-sell-all-1"))
    [(_, _, body, retryable, _)] = transport.calls
    assert body["quantity"] == HOLDING and body["side"] == "SELL" and body["orderType"] == "MARKET"
    assert "price" not in body and "orderAmount" not in body and retryable is False
    assert order.quantity is None and order.quantity_exact == Decimal(HOLDING)


def test_toss_sell_all_of_a_whole_holding_stays_a_whole_share_order(live_safety, live_sdk_env):
    """A holding of exactly 2 shares arrives as Decimal("2") and becomes the integer contract (LIMIT path)."""
    request = OrderRequest(symbol="AAPL", side="sell", quantity=Decimal("2"), price="190.05", market="US",
                           order_type="limit", client_key="us-sell-whole-1")
    assert request.quantity == 2 and type(request.quantity) is int and request.fractional is False
    transport = ContractTransport(clock=lambda: IN_SESSION)
    _toss(transport, live_safety, holding="2").place(request)
    assert transport.calls[0][2]["quantity"] == "2"


@pytest.mark.parametrize("venue,code", [("NASDAQ", "NASD"), ("NYSE", "NYSE"), ("AMEX", "AMEX")])
def test_kis_whole_share_us_sell_limit_reaches_the_vendor_with_the_symbol_exchange(live_safety, venue, code):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.post_order_overseas.return_value = {"json_data": {"rt_cd": "0", "output": {"ODNO": "43"}}}
    broker = KIS(account="fixture", market="US", venue=venue, client=client, api=api,
                 cash_provider=lambda scope: Decimal("1000"), safety=live_safety)
    order = broker.place_order(OrderRequest(symbol="TEST", side="sell", quantity=4, price="61.25", market="US",
                                            venue=venue, client_key=f"kis-sell-{code}"))
    api.post_order_overseas.assert_called_once_with(
        kis_account=scope, ticker="TEST", market=code, order_type="sell", order_qty=4, order_price=61.25)
    assert order.side == "sell" and order.quantity == 4


@pytest.mark.parametrize("venue", ["NASDAQ", "NYSE", "AMEX"])
def test_dbfi_whole_share_us_sell_limit_reaches_the_vendor(live_safety, venue):
    client = Mock()
    client.sell.return_value = {"rsp_cd": "00000", "Out": {"OrdNo": "43"}}
    broker = DBFI(account="fixture", market="US", venue=venue, client=client,
                  cash_provider=lambda client: Decimal("1000"), safety=live_safety)
    order = broker.place_order(OrderRequest(symbol="TEST", side="sell", quantity=4, price="61.25", market="US",
                                            venue=venue, client_key=f"db-sell-{venue}"))
    client.sell.assert_called_once_with(region="overseas", stock_code="TEST", quantity=4, price=61.25,
                                        price_type="1")
    client.buy.assert_not_called()
    assert order.side == "sell" and order.quantity == 4


@pytest.mark.parametrize("broker_type", [KIS, DBFI])
def test_kis_and_db_refuse_fractional_us_sells_before_the_vendor(live_safety, broker_type):
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    kwargs = dict(api=api) if broker_type is KIS else {}
    broker = broker_type(account="fixture", market="US", venue="NASDAQ", client=client,
                         cash_provider=lambda _: Decimal("1000"), safety=live_safety, **kwargs)
    with pytest.raises(FractionalNotSupportedError):
        broker.place_order(OrderRequest(symbol="TEST", side="sell", quantity=Decimal(HOLDING), market="US",
                                        order_type="market", venue="NASDAQ", client_key="frac-x"))
    api.post_order_overseas.assert_not_called()
    client.sell.assert_not_called()


def test_kis_us_market_sell_is_refused_before_the_vendor(live_safety):
    """KIS overseas order() is wired for LIMIT ("00") only — a MARKET sell is refused, never re-priced."""
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api,
                 cash_provider=lambda scope: Decimal("1000"),
                 safety=OrderSafety(dry_run=False, limits=RiskLimits(allow_orders=True, dry_run=False,
                                    per_order_krw_cap="10000000", daily_krw_cap="20000000"),
                                    risk_ledger=MemoryRiskLedger(), idempotency=MemoryIdempotencyStore(),
                                    kill_switch=KillSwitch(lambda broker, account: False),
                                    valuation=_valuation, market_order_allowed=lambda request: True))
    with pytest.raises(NotSupportedError):
        broker.place_order(OrderRequest(symbol="TEST", side="sell", quantity=2, market="US",
                                        order_type="market", venue="NASDAQ", client_key="kis-mkt"))
    api.post_order_overseas.assert_not_called()
