"""pycex-style JSON fixtures; every test is prohibited from opening the network."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.models import OrderRequest
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(broker: str, name: str):
    return json.loads((FIXTURES / broker / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network access is forbidden in tests")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def live_safety():
    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="100000", daily_krw_cap="200000"),
        risk_ledger=MemoryRiskLedger(),
        idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda broker, account: False),
    )


@pytest.fixture
def request_order():
    return OrderRequest(symbol="005930", side="buy", quantity=1, price="70000", client_key="fixture-intent")


@pytest.fixture
def kis(live_safety):
    client, api, scope = Mock(), Mock(), Mock()
    # Recorded fixtures declare their account mode; pykbroker refuses an undisclosed one.
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    scope.balance_all.return_value = load_fixture("kis", "balance")
    scope.amount.return_value = load_fixture("kis", "amount")
    api.get_price_domestic.return_value = load_fixture("kis", "quote")
    api.get_quote_domestic.return_value = load_fixture("kis", "book")
    api.get_open_orders_domestic.return_value = load_fixture("kis", "orders")
    api.post_order_domestic.return_value = load_fixture("kis", "submit")
    return KIS(account="fixture-account", client=client, api=api, safety=live_safety), client, api, scope


@pytest.fixture
def dbfi(live_safety):
    client, api = Mock(), Mock()
    client.get_stock_price.return_value = load_fixture("dbfi", "quote")
    client.get_order_book.return_value = load_fixture("dbfi", "book")
    client.get_transaction_history.return_value = load_fixture("dbfi", "orders")
    client.buy.return_value = load_fixture("dbfi", "submit")
    client.sell.return_value = load_fixture("dbfi", "submit")
    client.cancel.return_value = load_fixture("dbfi", "cancel")
    client.get_stock_balance.return_value = load_fixture("dbfi", "balance")
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {"MgnRat100pctOrdAbleAmt": "200000"}}
    api.get_stock_domestic.return_value = load_fixture("dbfi", "positions")
    return DBFI(account="fixture-account", client=client, api=api, safety=live_safety), client, api
