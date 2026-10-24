"""Offline stateful REST contract, through the registered real adapter and its existing client seam.

Field names and market-order values come from the pinned official JSON (fixtures/kiwoom/CONTRACT.md).
Matching, expiry clock, zero fees and ledger transitions are synthetic; this is not the Kiwoom mock service.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from pykbroker.brokers.kiwoom import KIWOOM_MOCK_BASE_URL, KIWOOM_TR_RATE_LIMITS, Kiwoom
from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    DuplicateOrderError,
    InvalidResponseError,
    NotSupportedError,
    RiskCapError,
    TransportError,
)
from pykbroker.experimental.kiwoom import KIWOOM_SPEC_VERIFIED
from pykbroker.factory import create_broker
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.rules.kr import KST
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.rate_limit import RateLimiter
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
AMEND_SAMPLE = json.loads((FIXTURES / "official" / "kt10002.json").read_text())
ERROR_CODES = {int(row["오류코드"]) for row in json.loads((FIXTURES / "official" / "contract_errors.json").read_text())}
END = {"cont-yn": "N"}


class FakeServer:
    """The existing post(path, headers, json) fake-client pattern, with deterministic account state."""

    def __init__(self):
        self.now = datetime(2026, 10, 24, 0, tzinfo=UTC)
        self.calls, self.orders, self.changes, self.tokens = [], {}, [], {}
        self.cash, self.position, self.cost = Decimal(1000000), 10, Decimal(700000)
        self.next_code = None
        self.uncertain_write = False
        self.enforce_rate = False
        self.windows = {}

    def factory(self, *, base_url):
        assert base_url == KIWOOM_MOCK_BASE_URL
        return self

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def post(self, path, *, headers, json):
        self.calls.append((path, dict(headers), dict(json)))
        assert headers["content-type"] == "application/json; charset=utf-8"
        if path == "/oauth2/token":
            assert json == {"grant_type": "client_credentials", "appkey": "fake-app", "secretkey": "fake-secret"}
            token = f"fake-token-{len(self.tokens) + 1}"
            expiry = self.now + timedelta(hours=24)
            self.tokens[f"Bearer {token}"] = expiry
            return {
                "return_code": 0,
                "token": token,
                "token_type": "bearer",
                "expires_dt": expiry.astimezone(KST).strftime("%Y%m%d%H%M%S"),
            }, dict(END)
        tr = headers["api-id"]
        assert path == ("/api/dostk/ordr" if tr.startswith("kt100") else "/api/dostk/acnt")
        if self.tokens.get(headers["authorization"], self.now) <= self.now:
            return {"return_code": 8005}, dict(END)
        if self.next_code is not None:
            code, self.next_code = self.next_code, None
            assert code in ERROR_CODES
            return {"return_code": code}, dict(END)
        if self.enforce_rate:
            # One deterministic fixed window for the documented mock per-TR/account/token rate.
            key = (headers["authorization"], tr, int(self.now.timestamp()))
            self.windows[key] = self.windows.get(key, 0) + 1
            if self.windows[key] > 1:
                return {"return_code": 1700}, dict(END)
        if tr in ("kt10000", "kt10001"):
            assert set(json) == {"dmst_stex_tp", "stk_cd", "ord_qty", "ord_uv", "trde_tp"}
            assert json["dmst_stex_tp"] == "KRX" and json["stk_cd"] == "005930"
            assert json["trde_tp"] in ("0", "3")
            assert (json["ord_uv"] == "") if json["trde_tp"] == "3" else int(json["ord_uv"]) > 0
            order_id = self.add_order(json, "buy" if tr == "kt10000" else "sell")
            if self.uncertain_write:
                self.uncertain_write = False
                raise TimeoutError("synthetic answer lost after acceptance")
            return {"return_code": 0, "ord_no": order_id, "dmst_stex_tp": "KRX"}, dict(END)
        if tr == "kt10002":
            assert set(json) == set(AMEND_SAMPLE["request"]) - {"mdfy_cond_uv"}
            original = self.orders[json["orig_ord_no"]]
            quantity = int(json["mdfy_qty"])
            assert original["symbol"] == json["stk_cd"] and 0 < quantity <= original["remaining"]
            assert json["dmst_stex_tp"] == "KRX" and int(json["mdfy_uv"]) > 0
            original["remaining"] -= quantity
            new_id = self.add_order(
                {"ord_qty": str(quantity), "ord_uv": json["mdfy_uv"], "trde_tp": "0", "stk_cd": original["symbol"]},
                original["side"],
                root=original["root"],
            )
            self.changes.append(
                {
                    "ord_no": new_id,
                    "ori_ord": json["orig_ord_no"],
                    "stk_cd": original["symbol"],
                    "dmst_stex_tp": "KRX",
                    "mdfy_cncl": "정정확인",
                    "cnfm_qty": str(quantity),
                }
            )
            return {
                **AMEND_SAMPLE["response"],
                "ord_no": new_id,
                "base_orig_ord_no": original["root"],
                "mdfy_qty": str(quantity),
            }, dict(END)
        if tr == "kt10003":
            assert set(json) == {"dmst_stex_tp", "orig_ord_no", "stk_cd", "cncl_qty"}
            original = self.orders[json["orig_ord_no"]]
            quantity = int(json["cncl_qty"])
            assert json["dmst_stex_tp"] == "KRX" and original["symbol"] == json["stk_cd"]
            assert 0 < quantity <= original["remaining"]
            original["remaining"] -= quantity
            cancel_id = f"{len(self.orders) + len(self.changes) + 100:07}"
            self.changes.append(
                {
                    "ord_no": cancel_id,
                    "ori_ord": json["orig_ord_no"],
                    "stk_cd": original["symbol"],
                    "dmst_stex_tp": "KRX",
                    "mdfy_cncl": "취소확인",
                    "cnfm_qty": str(quantity),
                }
            )
            return {
                "return_code": 0,
                "ord_no": cancel_id,
                "base_orig_ord_no": original["root"],
                "cncl_qty": str(quantity),
            }, dict(END)
        if tr == "kt00001":
            assert json == {"qry_tp": "2"}
            return {
                "return_code": 0,
                **{key: str(self.cash) for key in ("ord_alow_amt", "entr", "100stk_ord_alow_amt", "d2_entra")},
            }, dict(END)
        if tr == "kt00018":
            assert json == {"dmst_stex_tp": "KRX", "qry_tp": "1"}
            return {
                "return_code": 0,
                "acnt_evlt_remn_indv_tot": [
                    {
                        "stk_cd": "A005930",
                        "stk_nm": "삼성전자",
                        "rmnd_qty": str(self.position),
                        "trde_able_qty": str(self.position),
                        "pur_pric": str(self.cost / self.position),
                        "cur_prc": "70000",
                        "evlt_amt": str(self.position * 70000),
                    }
                ],
            }, dict(END)
        if tr == "ka10075":
            assert json in (
                {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1"},
                {"all_stk_tp": "1", "trde_tp": "0", "stex_tp": "1", "stk_cd": "005930"},
            )
            rows = [
                {
                    "ord_no": key,
                    "stk_cd": row["symbol"],
                    "ord_qty": str(row["quantity"]),
                    "cntr_qty": str(row["filled"]),
                    "oso_qty": str(row["remaining"]),
                    "ord_pric": row["price"],
                    "cntr_tot_amt": str(row["amount"]),
                    "io_tp_nm": "매수" if row["side"] == "buy" else "매도",
                    "trde_tp": row["type"],
                }
                for key, row in self.orders.items()
                if row["remaining"]
            ]
            return {"return_code": 0, "oso": rows}, dict(END)
        assert tr == "kt00007"
        assert json == {
            "ord_dt": self.now.astimezone(KST).strftime("%Y%m%d"),
            "qry_tp": "1",
            "stk_bond_tp": "1",
            "sell_tp": "0",
            "stk_cd": "005930",
            "dmst_stex_tp": "KRX",
        }
        rows = [
            {
                "ord_no": key,
                "ori_ord": "0000000",
                "stk_cd": row["symbol"],
                "ord_qty": str(row["quantity"]),
                "cntr_qty": str(row["filled"]),
                "ord_remnq": str(row["remaining"]),
                "mdfy_cncl": "",
                "cntr_uv": str(row["amount"] / row["filled"]) if row["filled"] else "0",
            }
            for key, row in self.orders.items()
        ]
        # Confirmation rows are synthetic and distinct from the child order's own ledger row.
        changes = [{**row, "ord_no": f"{9000000 + index:07}"} for index, row in enumerate(self.changes)]
        return {"return_code": 0, "acnt_ord_cntr_prps_dtl": rows + changes}, dict(END)

    def add_order(self, body, side, root=None):
        order_id = f"{len(self.orders) + 1:07}"
        quantity = int(body["ord_qty"])
        self.orders[order_id] = {
            "symbol": body["stk_cd"],
            "side": side,
            "quantity": quantity,
            "remaining": quantity,
            "filled": 0,
            "amount": Decimal(0),
            "price": body["ord_uv"] or "0",
            "type": "시장가" if body["trde_tp"] == "3" else "지정가",
            "root": root or order_id,
        }
        return order_id

    def fill(self, order_id, quantity, price):
        row = self.orders[order_id]
        assert 0 < quantity <= row["remaining"]
        amount = quantity * Decimal(price)
        row["filled"] += quantity
        row["remaining"] -= quantity
        row["amount"] += amount
        if row["side"] == "buy":
            self.cash -= amount
            self.position += quantity
            self.cost += amount
        else:
            self.cost -= self.cost / self.position * quantity
            self.cash += amount
            self.position -= quantity


def safety(**changes):
    return OrderSafety(
        **{
            "dry_run": False,
            "limits": RiskLimits(
                allow_orders=True, dry_run=False, per_order_krw_cap="10000000", daily_krw_cap="100000000"
            ),
            "risk_ledger": MemoryRiskLedger(),
            "idempotency": MemoryIdempotencyStore(),
            "kill_switch": KillSwitch(lambda broker, account: False),
            "valuation": lambda request: Decimal(100000) * request.quantity,
            "market_order_allowed": lambda request: True,
            **changes,
        }
    )


def connect(server, guard=None, *, issued=None, **changes):
    token = issued or Kiwoom.issue_token(
        app_key="fake-app", secret_key="fake-secret", virtual=True, client_factory=server.factory
    )
    return create_broker(
        "kiwoom",
        account="fake-account",
        virtual=True,
        client_options={**token, "app_key": "fake-app"},
        client_factory=server.factory,
        codes={"exchange": "KRX", "query_type": "1"},
        safety=guard or safety(),
        rate_limiter=RateLimiter({tr: 100000 for tr in KIWOOM_TR_RATE_LIMITS}),
        clock=server.clock,
        **changes,
    )


def request(**changes):
    return OrderRequest(
        **{"symbol": "005930", "side": "buy", "quantity": 3, "price": "70000", "client_key": "place-intent", **changes}
    )


def amendment(order, **changes):
    return AmendRequest(
        **{
            "order_id": order.id,
            "symbol": "005930",
            "side": "buy",
            "quantity": 2,
            "price": "69000",
            "client_key": "amend-intent",
            **changes,
        }
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind", ["limit", "market"])
def test_cash_market_limit_buy_sell_and_fill(side, kind):
    server = FakeServer()
    broker = connect(server)
    assert type(broker) is Kiwoom and KIWOOM_SPEC_VERIFIED is False
    order = broker.place_order(request(side=side, order_type=kind, price=None if kind == "market" else "70000"))
    assert (order.status, order.order_type, order.virtual) == ("accepted", kind, True)
    assert (broker.status(order).status, broker.list_orders()[0].remaining) == ("open", 3)
    server.fill(order.id, 3, "70000")
    filled = broker.status(order)
    # kt00007 cntr_uv is not documented as a multi-fill average; existing mapping leaves it unknown.
    assert (filled.status, filled.filled, filled.remaining, filled.fill_price) == ("filled", 3, 0, None)
    assert broker.list_orders() == []
    assert broker.get_positions()[0].quantity == (13 if side == "buy" else 7)
    assert broker.get_balance().cash_available == (Decimal(790000) if side == "buy" else Decimal(1210000))


def test_partial_fill_amend_remaining_partial_fill_cancel_reconciles_cash_positions_ledger():
    server = FakeServer()
    broker = connect(server)
    original = broker.place_order(request())
    server.fill(original.id, 1, "70000")
    partial = broker.list_orders("005930")[0]
    assert (partial.status, partial.filled, partial.remaining, partial.fill_price) == ("partial", 1, 2, Decimal(70000))
    child = broker.amend_order(amendment(original))
    assert child.id != original.id and child.side is None and child.order_type is None
    assert child.status == "accepted" and child.quantity == 2
    assert broker.status(original).status == "unknown"  # real amendment ledger transitions remain unverified
    server.fill(child.id, 1, "69000")
    partial = broker.status(child)
    assert (partial.status, partial.filled, partial.remaining, partial.fill_price) == ("partial", 1, 1, Decimal(69000))
    broker.cancel_order(CancelRequest(order_id=child.id, symbol=child.symbol, quantity=1, client_key="cancel-intent"))
    final = broker.status(child)
    assert (final.status, final.filled, final.remaining) == ("cancelled", 1, 0)
    assert broker.list_orders() == []
    assert broker.get_positions()[0].quantity == 12
    balance = broker.get_balance()
    assert (balance.cash_available, balance.position_value) == (Decimal(861000), Decimal(840000))


def test_expiry_explicit_reissue_same_guard_never_replays_order():
    server = FakeServer()
    guard = safety()
    broker = connect(server, guard)
    original = broker.place_order(request(quantity=1))
    server.advance(24 * 60 * 60)
    count = len(server.calls)
    with pytest.raises(ConfigurationError):
        broker.place_order(request(client_key="expired-intent", quantity=1))
    assert len(server.calls) == count
    refreshed = connect(server, guard)
    with pytest.raises(DuplicateOrderError):
        refreshed.place_order(request(quantity=1))
    # Expired local attempts also retain the original safety claim; the caller must reconcile/change intent.
    with pytest.raises(DuplicateOrderError):
        refreshed.place_order(request(client_key="expired-intent", quantity=1))
    new = refreshed.place_order(request(client_key="new-intent", quantity=1))
    assert new.id != original.id and len(server.orders) == 2 and len(server.tokens) == 2


@pytest.mark.parametrize("code", [1511, 1902, 8005, 1700, 1701, 1702, 1999])
@pytest.mark.parametrize("operation", ["place", "amend", "cancel"])
def test_official_errors_no_write_replay_and_retained_client_key(code, operation):
    server = FakeServer()
    broker = connect(server)
    original = broker.place_order(request())

    def mutate():
        if operation == "place":
            return broker.place_order(request(client_key="second-intent"))
        if operation == "amend":
            return broker.amend_order(amendment(original))
        cancel = CancelRequest(order_id=original.id, symbol=original.symbol, quantity=1, client_key="cancel-intent")
        return broker.cancel_order(cancel)

    server.next_code = code
    count = len(server.calls)
    with pytest.raises(InvalidResponseError if code == 1999 else BrokerRejectedError):
        mutate()
    assert len(server.calls) == count + 1
    with pytest.raises(DuplicateOrderError):
        mutate()
    assert len(server.calls) == count + 1 and len(server.orders) == 1


def test_rate_limit_window_on_fake_server_and_next_window_read():
    server = FakeServer()
    broker = connect(server)
    server.enforce_rate = True
    assert broker.available_cash() == Decimal(1000000)
    with pytest.raises(BrokerRejectedError):
        broker.available_cash()
    server.advance(1)
    assert broker.available_cash() == Decimal(1000000)
    assert len(server.calls) == 4  # token plus exactly three reads, no retries


def test_accepted_order_with_lost_reply_is_not_duplicated():
    server = FakeServer()
    broker = connect(server)
    server.uncertain_write = True
    with pytest.raises(TransportError):
        broker.place_order(request())
    with pytest.raises(DuplicateOrderError):
        broker.place_order(request())
    assert len(server.orders) == 1
    assert broker.list_orders()[0].id == next(iter(server.orders))


def test_amendment_action_claim_is_separate_from_place_cancel_and_cannot_repeat():
    server = FakeServer()
    broker = connect(server)
    order = broker.place_order(request(client_key="shared"))
    changed = broker.amend_order(amendment(order, client_key="shared"))
    count = len(server.calls)
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amendment(changed, client_key="shared"))
    assert len(server.calls) == count
    broker.cancel_order(CancelRequest(order_id=changed.id, symbol=changed.symbol, quantity=2, client_key="shared"))
    assert broker.status(changed).status == "cancelled"


@pytest.mark.parametrize("guard", [OrderSafety, lambda: safety(kill_switch=KillSwitch(lambda broker, account: True))])
def test_amendment_dry_run_and_emergency_stop_prevent_post(guard):
    server = FakeServer()
    broker = connect(server, guard())
    count = len(server.calls)
    with pytest.raises(BrokerError):
        broker.amend_order(
            AmendRequest(order_id="0000001", symbol="005930", side="buy", quantity=1, price="70000", client_key="a")
        )
    assert len(server.calls) == count


def test_market_order_still_requires_session_authorization_and_conservative_valuation():
    for guard in (safety(market_order_allowed=None), safety(valuation=None), OrderSafety()):
        server = FakeServer()
        broker = connect(server, guard)
        count = len(server.calls)
        with pytest.raises(BrokerError):
            broker.place_order(request(order_type="market", price=None))
        assert len(server.calls) == count


def test_amendment_keeps_full_notional_risk_check_before_post():
    server = FakeServer()
    guard = safety(
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="100000", daily_krw_cap="1000000"),
        valuation=None,
    )
    broker = connect(server, guard)
    item = AmendRequest(order_id="0000001", symbol="005930", side="buy", quantity=2, price="69000", client_key="a")
    count = len(server.calls)
    with pytest.raises(RiskCapError):
        broker.amend_order(item)
    assert len(server.calls) == count


def test_closed_or_us_broker_cannot_send_amendment():
    for kwargs in ({}, {"market": "US", "venue": "NASDAQ"}):
        server = FakeServer()
        broker = connect(server, **kwargs)
        if not kwargs:
            broker.close()
        count = len(server.calls)
        item = AmendRequest(order_id="0000001", symbol="005930", side="buy", quantity=1, price="69000", client_key="a")
        with pytest.raises(NotSupportedError if kwargs else ConfigurationError):
            broker.amend_order(item)
        assert len(server.calls) == count


def test_amendment_revalidates_models_and_cannot_be_submitted_as_new_order():
    server = FakeServer()
    broker = connect(server)
    item = AmendRequest(order_id="0000001", symbol="005930", side="buy", quantity=1, price="70000", client_key="a")
    count = len(server.calls)
    with pytest.raises(ValidationError):
        broker.place_order(item)
    for invalid in (item.model_copy(update={"order_type": "market"}), item.model_copy(update={"quantity": 0})):
        with pytest.raises(ValidationError):
            broker.amend_order(invalid)
    for invalid in (item.model_copy(update={"order_id": "0000000"}), item.model_copy(update={"price": Decimal("1.5")})):
        with pytest.raises(ConfigurationError):
            broker.amend_order(invalid)
    with pytest.raises(NotSupportedError):
        broker.amend_order(item.model_copy(update={"market": "US"}))
    assert len(server.calls) == count


@pytest.mark.parametrize(
    "changes", [{"ord_no": "0000000"}, {"mdfy_qty": "1"}, {"dmst_stex_tp": "NXT"}, {"base_orig_ord_no": "invalid"}]
)
def test_mismatched_amendment_ack_is_unknown_without_retry(changes):
    server = FakeServer()
    broker = connect(server)
    original = broker.place_order(request())
    post = server.post

    def altered(path, *, headers, json):
        body, response_headers = post(path, headers=headers, json=json)
        return ({**body, **changes} if headers.get("api-id") == "kt10002" else body), response_headers

    server.post = altered
    with pytest.raises(InvalidResponseError):
        broker.amend_order(amendment(original))
    with pytest.raises(DuplicateOrderError):
        broker.amend_order(amendment(original))
    assert len(server.orders) == 2
