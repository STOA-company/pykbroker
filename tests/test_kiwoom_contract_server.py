"""Multi-turn walks of the production Kiwoom broker against the spec-derived contract server (no network)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pykbroker.brokers import kiwoom as module
from pykbroker.brokers.kiwoom import Kiwoom
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError
from pykbroker.factory import create_broker
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.rate_limit import RateLimiter
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

from .kiwoom_contract_server import SCHEMA, KiwoomContractServer

START = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)  # 09:30 KST
KST = timedelta(hours=9)
FAST = RateLimiter({tr: 1000 for tr in module.KIWOOM_TR_RATE_LIMITS})


class World:
    def __init__(self, **server_kwargs):
        self.utc = START
        self.server = KiwoomContractServer(now=(START + KST).replace(tzinfo=None), **server_kwargs)

    def advance(self, delta):
        self.utc += delta
        self.server.advance(delta)

    def factory(self, *, base_url):
        self.server.base_url = base_url
        return self.server

    def issue(self, virtual=True):
        return Kiwoom.issue_token(app_key="fixture-app", secret_key="fixture-secret", virtual=virtual,
                                  client_factory=self.factory)

    def broker(self, virtual=True, **kwargs):
        issued = self.issue(virtual=virtual)
        safety = OrderSafety(
            dry_run=False,
            limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="1000000",
                              daily_krw_cap="2000000"),
            risk_ledger=MemoryRiskLedger(), idempotency=MemoryIdempotencyStore(),
            kill_switch=KillSwitch(lambda broker, account: False),
        )
        return create_broker(
            "kiwoom", account="fixture-account", client_factory=self.factory, virtual=virtual, safety=safety,
            client_options={**issued, "app_key": "fixture-app"},
            codes={"exchange": "KRX", "query_type": "1"}, rate_limiter=kwargs.pop("rate_limiter", FAST),
            clock=lambda: self.utc, **kwargs,
        )


def buy(key, qty=5, price="60000"):
    return OrderRequest(symbol="005930", side="buy", quantity=qty, price=price, client_key=key)


def test_buy_partial_fill_cancel_balance_walk():
    world = World()
    broker = world.broker()
    server = world.server
    assert server.base_url == module.KIWOOM_MOCK_BASE_URL

    cash0 = broker.available_cash()
    assert cash0 == Decimal(1_000_000)
    assert broker.get_quote("005930").last == Decimal(72_000)

    # turn 1: buy 5 -> accepted, shows as open, nothing held yet
    order = broker.place_order(buy("k1"))
    assert order.status == "accepted"
    assert [o.status for o in broker.list_orders("005930")] == ["open"]
    assert broker.get_positions() == []

    # turn 2: 2 of 5 fill at 59,900 (price improvement) -> partial, average fill is reported
    server.fill(order.id, 2, 59_900)
    [partial] = broker.list_orders("005930")
    assert (partial.status, partial.filled, partial.remaining, partial.fill_price) == (
        "partial", 2, 3, Decimal(59_900))
    assert broker.status(order).status == "partial"
    [position] = broker.get_positions()
    assert (position.symbol, position.quantity, position.average_price) == ("005930", 2, Decimal(59_900))
    assert broker.available_cash() == Decimal(1_000_000 - 2 * 59_900)

    # turn 3: cancel the remaining 3 -> confirmed cancel keeps the 2 fills
    cancelled = broker.cancel_order(CancelRequest(order_id=order.id, symbol="005930", quantity=3, client_key="c1"))
    assert cancelled.status == "accepted"  # an acknowledgement is not the final state
    assert broker.list_orders("005930") == []
    final = broker.status(order)
    assert (final.status, final.filled, final.remaining) == ("cancelled", 2, 0)

    # turn 4: balance reflects only the filled part
    balance = broker.get_balance()
    assert balance.cash_available == Decimal(1_000_000 - 2 * 59_900)
    assert balance.position_value == Decimal(2 * 72_000)
    assert [path for path, _ in server.calls if path != "/oauth2/token"].count("/api/dostk/ordr") == 2


def test_full_fill_and_sell_back():
    world = World()
    broker = world.broker()
    order = broker.place_order(buy("k1", qty=3))
    world.server.fill(order.id, 3, 60_000)
    assert broker.status(order).status == "filled"
    assert broker.list_orders() == []
    sell = broker.place_order(
        OrderRequest(symbol="005930", side="sell", quantity=3, price="61000", client_key="s1"))
    world.server.fill(sell.id, 3, 61_000)
    assert broker.status(sell).status == "filled"
    assert broker.get_positions() == []
    assert broker.available_cash() == Decimal(1_000_000 + 3_000)


def test_paged_open_orders_follow_cont_yn_next_key():
    world = World(page_size=2, cash=100_000_000)
    broker = world.broker()
    for i in range(5):
        broker.place_order(buy(f"k{i}", qty=1))
    assert len(broker.list_orders()) == 5
    pages = [c for c in world.server.calls if c[0] == "/api/dostk/acnt"]
    assert len(pages) == 3


def test_token_expiry_is_refused_before_the_wire_and_by_the_server():
    world = World(token_lifetime=timedelta(hours=1))
    broker = world.broker()
    assert broker.available_cash()
    world.advance(timedelta(minutes=56))  # inside the 5 minute safety margin: the broker refuses, no call is made
    before = len(world.server.calls)
    with pytest.raises(ConfigurationError):
        broker.available_cash()
    assert len(world.server.calls) == before
    # a broker that does not know the expiry hits the server's 8005 and reports a refusal, never a success
    stale = world.broker()
    world.advance(timedelta(hours=2))
    stale._expires_at = None
    with pytest.raises(BrokerRejectedError):
        stale.available_cash()


def test_a_reissued_token_works_again():
    world = World(token_lifetime=timedelta(hours=1))
    first = world.broker()
    world.advance(timedelta(hours=2))
    with pytest.raises(ConfigurationError):
        first.available_cash()
    assert world.broker().available_cash() == Decimal(1_000_000)


def test_rate_limit_overrun_is_a_refusal_and_a_write_is_not_retried():
    world = World()
    broker = world.broker()
    world.server.limits = {"kt10000": 1}
    broker.place_order(buy("k1", qty=1))
    with pytest.raises(BrokerRejectedError):
        broker.place_order(buy("k2", qty=1))
    assert sum(1 for _, b in world.server.calls if b.get("ord_qty")) == 2  # exactly one attempt each, no retry
    world.server.tick()
    broker.place_order(buy("k3", qty=1))
    assert len(world.server.orders) == 2


def test_business_rejection_and_unexpected_error_on_a_write():
    from pykbroker.exceptions import InvalidResponseError

    world = World(cash=10_000)
    broker = world.broker()
    with pytest.raises(BrokerRejectedError):
        broker.place_order(buy("k1", qty=1))  # more than the orderable cash
    assert not world.server.orders
    world.server.cash = 1_000_000
    world.server.fail_next["kt10000"] = (1999, "")
    with pytest.raises(InvalidResponseError):  # 1999 on a write is unknown, never «safe to resubmit»
        broker.place_order(buy("k2", qty=1))


def test_every_request_the_broker_sends_matches_the_official_required_fields():
    world = World(page_size=50)
    broker = world.broker()
    order = broker.place_order(buy("k1"))
    world.server.fill(order.id, 1)
    broker.status(order)
    broker.list_orders()
    broker.get_balance()
    broker.get_rules("005930")
    broker.cancel_order(CancelRequest(order_id=order.id, symbol="005930", quantity=4, client_key="c1"))
    # the server answers 1511/1517 for a missing/oversized/unknown field; none of that may have happened
    assert world.server.calls
    assert world.server.invalid == []
    assert set(SCHEMA) >= {"kt10000", "kt10003", "ka10075", "kt00007", "kt00018", "kt00001", "ka10001", "ka10004"}


def test_the_server_itself_enforces_the_spec():
    world = World()
    token = world.issue()["token"]
    headers = {"authorization": f"Bearer {token}", "api-id": "kt10000"}
    path = "/api/dostk/ordr"
    assert world.server.post(path, headers=headers, json={"stk_cd": "005930"})[0]["return_code"] == 1511
    assert world.server.post(path, headers={**headers, "api-id": "ka10075"}, json={})[0]["return_code"] == 1504
    assert world.server.post(path, headers={"api-id": "kt10000"}, json={})[0]["return_code"] == 1513
    body = {"dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "1", "ord_uv": "6", "trde_tp": "0", "x": "1"}
    assert world.server.post(path, headers=headers, json=body)[0]["return_code"] == 1517
    assert world.server.post("/oauth2/token", headers={}, json={"grant_type": "x", "appkey": "a",
                                                                 "secretkey": "b"})[0]["return_code"] == 8012
