"""미국 주간거래(overnight) — 계약 모의로 KIS·NH·토스 세 곳을 접수→송신 1회→부분체결→정정(가격)→취소→04:00 만료까지 걷는다.

공식 사양: KIS examples_llm/overseas_stock/daytime_order(TTTS6036U 매수·6037U 매도, ORD_DVSN 00 만)·
daytime_order_rvsecncl(TTTS6038U 01 정정·02 취소) · NH gbstock 호가유형 63 = 주간거래(지정가) · 토스 v1.2.24
(주문에 세션 칸 없음, US modify 는 가격만). 네트워크 0 — 전부 이 파일 안의 가짜 서버다.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import (
    DAYTIME_AMEND_PATH,
    DAYTIME_ORDER_PATH,
    DAYTIME_QUOTE_EXCD,
    DAYTIME_TR,
    KIS,
)
from pykbroker.brokers.nh import NH, NH_US_DAYTIME_LIMIT
from pykbroker.brokers.toss import Toss
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TossRejectedError,
)
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

from .test_nh_order_contract import reply
from .toss_contract_mock import ContractMockTossSdk

REGULAR_KIS_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"


class VenueLedger:
    """한 증권사 안의 주문장 — 접수·부분체결·정정·취소·04:00 자동 취소를 흉내 낸다."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.seq = 0

    def place(self, symbol, side, qty, price) -> str:
        self.seq += 1
        order_id = str(1000 + self.seq)
        self.orders[order_id] = dict(symbol=symbol, side=side, qty=qty, filled=0, price=Decimal(str(price)), state="open")
        return order_id

    def fill(self, order_id, n):
        row = self.orders[order_id]
        row["filled"] += n
        row["state"] = "filled" if row["filled"] == row["qty"] else "open"

    def amend(self, order_id, price):
        self.orders[order_id]["price"] = Decimal(str(price))

    def cancel(self, order_id):
        self.orders[order_id]["state"] = "cancelled"

    def expire_all(self):
        """04:00 ET — 주간거래 미체결은 증권사가 자동 취소한다."""
        for row in self.orders.values():
            if row["state"] == "open":
                row["state"] = "cancelled"

    def open_rows(self):
        return {k: v for k, v in self.orders.items() if v["state"] == "open"}


def request_order(session="overnight", side="buy", **changes):
    return OrderRequest(
        **{"symbol": "AAPL", "side": side, "quantity": 3, "price": "190.5", "market": "US", "venue": "NASDAQ",
           "session": session, "client_key": changes.pop("client_key", "k-place"), **changes}
    )


def money(live_safety):
    live_safety.valuation = lambda request: Decimal("1000")
    return live_safety


# --------------------------------------------------------------------------- 모델·선언

def test_overnight_is_only_a_whole_share_us_limit_order():
    ok = request_order()
    assert ok.session == "overnight"
    for bad in (
        dict(order_type="market", price=None),  # 시장가 거절(주간 = 지정가만)
        dict(market="KR", venue="KRX", symbol="005930", price="70000"),
    ):
        with pytest.raises(ValueError):
            request_order(**bad)
    assert OrderRequest(symbol="AAPL", side="buy", quantity=1, price="1", market="US", client_key="x").session == "regular"


def test_every_other_broker_declares_regular_only(live_safety):
    assert DBFI.__mro__  # 선언 없는 어댑터는 기본값 ("regular",) 이다 — 기본값이 바뀌면 여기서 빨강
    from pykbroker.models import Capabilities

    assert Capabilities().sessions == ("regular",) and Capabilities().supports_amend is False
    for module, name in (("dbfi", "DBFI"), ("kb", "KB"), ("ls", "LS"), ("kiwoom", "Kiwoom")):
        cls = getattr(__import__(f"pykbroker.brokers.{module}", fromlist=[name]), name)
        assert not hasattr(cls, "amend_order"), name  # 정정은 사양이 있는 곳에만 있다
        probe = cls.__new__(cls)
        probe.market, probe.virtual, probe._bound_virtual, probe._allow_live_orders, probe._spec_verified = "US", False, False, True, True
        try:
            sessions = probe.capabilities().sessions
        except Exception:
            continue  # 선언조차 못 하는 증권사는 닫혀 있다
        assert "overnight" not in sessions, name


def test_kis_declares_overnight_only_for_real_us_accounts():
    def caps(market, virtual):
        kis = KIS(account="a", market=market, venue="NASDAQ" if market == "US" else None, virtual=virtual,
                  client=SimpleNamespace(key=SimpleNamespace(virtual_account=virtual)), api=object())
        return kis.capabilities()

    assert caps("US", False).sessions == ("regular", "overnight") and caps("US", False).supports_amend
    assert caps("US", True).sessions == ("regular",) and not caps("US", True).supports_amend
    assert caps("KR", False).sessions == ("regular",)


# --------------------------------------------------------------------------- KIS 계약 모의

class KisDaytimeServer:
    """KIS 주간 TR 계약: 경로·tr_id·본문 칸·ORD_DVSN 00 만. 어기면 rt_cd 1(거절)."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, path, headers=None, body=None, params=None, response=None):
        tr_id = (headers or {}).get("tr_id")
        self.calls.append((path, tr_id, dict(body or {})))
        reject = {"rt_cd": "1", "msg_cd": "CONTRACT-REJECT", "msg1": "rejected by contract mock"}
        if path == DAYTIME_ORDER_PATH:
            side = {"TTTS6036U": "buy", "TTTS6037U": "sell"}.get(tr_id)
            need = {"CANO", "ACNT_PRDT_CD", "OVRS_EXCG_CD", "PDNO", "ORD_QTY", "OVRS_ORD_UNPR", "CTAC_TLNO",
                    "MGCO_APTM_ODNO", "ORD_SVR_DVSN_CD", "ORD_DVSN"}
            if side is None or not need <= set(body) or body["ORD_DVSN"] != "00" or body["ORD_SVR_DVSN_CD"] != "0":
                return reject
            if body["OVRS_EXCG_CD"] not in {"NASD", "NYSE", "AMEX"}:
                return reject
            order_id = self.ledger.place(body["PDNO"], side, int(body["ORD_QTY"]), body["OVRS_ORD_UNPR"])
            return {"rt_cd": "0", "output": {"ODNO": order_id, "ORD_TMD": "100000"}}
        if path == DAYTIME_AMEND_PATH:
            need = {"CANO", "ACNT_PRDT_CD", "OVRS_EXCG_CD", "PDNO", "ORGN_ODNO", "RVSE_CNCL_DVSN_CD", "ORD_QTY",
                    "OVRS_ORD_UNPR", "CTAC_TLNO", "MGCO_APTM_ODNO", "ORD_SVR_DVSN_CD"}
            if tr_id != "TTTS6038U" or not need <= set(body) or body["RVSE_CNCL_DVSN_CD"] not in {"01", "02"}:
                return reject
            row = self.ledger.orders.get(body["ORGN_ODNO"])
            if row is None or row["state"] != "open":
                return reject  # 이미 취소·체결된 주문 — 두 번째 취소는 증권사가 거절
            if body["RVSE_CNCL_DVSN_CD"] == "01":
                self.ledger.amend(body["ORGN_ODNO"], body["OVRS_ORD_UNPR"])
            else:
                self.ledger.cancel(body["ORGN_ODNO"])
            return {"rt_cd": "0", "output": {"ODNO": "9" + body["ORGN_ODNO"]}}
        if path == REGULAR_KIS_ORDER_PATH:
            return reject  # 주간 시간에 정규장 TR 은 닫혀 있다 — 이 길로 가면 안 된다
        raise AssertionError(path)


def kis_fixture(live_safety):
    ledger = VenueLedger()
    server = KisDaytimeServer(ledger)
    scope = SimpleNamespace(
        client=server, account=SimpleNamespace(build_body=lambda fields: {"CANO": "12345678", "ACNT_PRDT_CD": "01", **fields})
    )
    vendor = SimpleNamespace(client=server, key=SimpleNamespace(virtual_account=False), account=lambda _a: scope)

    def open_orders(kis_account, market):
        rows = [{"odno": k, "pdno": v["symbol"], "nccs_qty": str(v["qty"] - v["filled"]), "ord_gno_brno": "06010"}
                for k, v in ledger.open_rows().items()]
        return {"json_data": {"rt_cd": "0", "output": rows}}

    def all_orders(kis_account, code):
        rows = [{"odno": k, "pdno": v["symbol"], "ft_ord_qty": str(v["qty"]), "ft_ccld_qty": str(v["filled"]),
                 "nccs_qty": str(v["qty"] - v["filled"] if v["state"] == "open" else 0),
                 "rvse_cncl_dvsn": "02" if v["state"] == "cancelled" else ""}
                for k, v in ledger.orders.items()]
        return {"json_data": {"rt_cd": "0", "output": rows}}

    api = SimpleNamespace(
        get_open_orders_overseas_all=open_orders,
        get_orders_overseas_all=all_orders,
        post_order_overseas=lambda **kw: pytest.fail("regular US order TR must not be used for overnight"),
    )
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=vendor, api=api, safety=money(live_safety))
    return broker, server, ledger


def test_kis_overnight_walk_place_partial_amend_cancel_expire(live_safety):
    broker, server, ledger = kis_fixture(live_safety)
    placed = broker.place_order(request_order())
    assert [(p, t) for p, t, _ in server.calls] == [(DAYTIME_ORDER_PATH, "TTTS6036U")]  # 송신 1회
    body = server.calls[0][2]
    assert (body["ORD_DVSN"], body["OVRS_EXCG_CD"], body["PDNO"], body["ORD_QTY"], body["OVRS_ORD_UNPR"]) == (
        "00", "NASD", "AAPL", 3, 190.5)
    assert placed.status == "accepted"
    ledger.fill(placed.id, 1)  # 부분체결
    seen = broker.status(placed)
    assert (seen.status, seen.filled, seen.remaining) == ("partial", 1, 2)
    amended = broker.amend_order(AmendRequest(order_id=placed.id, symbol="AAPL", side="buy", quantity=2, price="191.0",
                                              market="US", venue="NASDAQ", session="overnight", client_key="k-amend"))
    assert amended.status == "accepted" and ledger.orders[placed.id]["price"] == Decimal("191.0")
    assert server.calls[-1][1] == "TTTS6038U" and server.calls[-1][2]["RVSE_CNCL_DVSN_CD"] == "01"
    assert server.calls[-1][2]["ORD_QTY"] == 2  # 남은 수량 그대로
    broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=2, market="US", venue="NASDAQ",
                                      session="overnight", client_key="k-cancel"))
    assert server.calls[-1][1] == "TTTS6038U" and server.calls[-1][2]["RVSE_CNCL_DVSN_CD"] == "02"
    assert ledger.orders[placed.id]["state"] == "cancelled"
    assert all(p != REGULAR_KIS_ORDER_PATH for p, _, _ in server.calls)


def test_kis_overnight_sell_uses_6037_and_expiry_leaves_no_second_cancel(live_safety):
    broker, server, ledger = kis_fixture(live_safety)
    placed = broker.place_order(request_order(side="sell", client_key="k-sell"))
    assert server.calls[-1][1] == "TTTS6037U" == DAYTIME_TR["sell"]
    ledger.expire_all()  # 04:00 ET 증권사 자동 취소
    sent = len(server.calls)
    with pytest.raises(NotSupportedError):  # 장부에 열린 주문이 없다 → 취소를 보내지 않는다(두 번 취소 0)
        broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=3, market="US", venue="NASDAQ",
                                          session="overnight", client_key="k-cancel2"))
    assert len(server.calls) == sent
    assert broker.status(placed).status == "cancelled"


def test_kis_regular_request_never_takes_the_daytime_tr(live_safety):
    broker, server, _ = kis_fixture(live_safety)
    seen = {}
    broker._api.post_order_overseas = lambda **kw: seen.update(kw) or {"json_data": {"rt_cd": "0", "output": {"ODNO": "7"}}}
    broker.place_order(request_order(session="regular", client_key="k-reg"))
    assert seen["market"] == "NASD" and seen["order_price"] == 190.5 and server.calls == []


def test_kis_wrong_daytime_answer_is_rejected_not_retried(live_safety):
    broker, server, _ = kis_fixture(live_safety)
    server.request = lambda *a, **k: {"rt_cd": "1", "msg_cd": "X", "msg1": "closed"}
    with pytest.raises(BrokerRejectedError):
        broker.place_order(request_order(client_key="k-rej"))


def test_kis_overnight_quote_reads_the_daytime_exchange_codes():
    assert DAYTIME_QUOTE_EXCD == {"NASDAQ": "BAQ", "NYSE": "BAY", "AMEX": "BAA"}
    seen = []
    api = SimpleNamespace(
        get_price_overseas_detail=lambda **kw: seen.append(kw["excd"]) or {"json_data": {"rt_cd": "0", "output": {"last": "190"}}},
        get_quote_overseas=lambda **kw: seen.append(kw["excd"]) or {"json_data": {"rt_cd": "0", "output2": {"pbid1": "189", "pask1": "191"}}},
    )
    kis = KIS(account="a", market="US", venue="NYSE", client=SimpleNamespace(key=SimpleNamespace(virtual_account=False), account=lambda _a: object()), api=api)
    assert kis.get_quote("KO", market="US", session="overnight").ask == Decimal("191")
    assert seen == ["BAY", "BAY"]
    kis.get_quote("KO", market="US")
    assert seen[2:] == ["NYS", "NYS"]  # 정규장 시세는 그대로


# --------------------------------------------------------------------------- NH 계약 모의

NH_ORDER = "/gbstock/order/v1/"


def nh_fixture(live_safety):
    ledger = VenueLedger()
    calls = []

    class Server:
        def __init__(self, base_url):
            self.base_url = base_url

        def post(self, path, *, headers, data):
            body = json.loads(data)["Input_0"]
            calls.append((path, body))
            if path in (NH_ORDER + "buy", NH_ORDER + "sell"):
                if body["ahi_nmn_pr_tp_cd"] not in ("00", "63") or body["fc_sec_trd_nat_cd"] != "200":
                    return reply(code="E0001", message="호가유형 오류")
                side = path.rsplit("/", 1)[1]
                order_id = ledger.place(body["iem_cd"], side, body["orr_qty"], body["fc_orr_uit_pr"])
                ledger.orders[order_id]["ahi"] = body["ahi_nmn_pr_tp_cd"]
                return reply({"orr_no": int(order_id)})
            if path == NH_ORDER + "modify":
                if set(body) != {"act_no", "fc_sec_trd_nat_cd", "iem_cd", "org_orr_no", "fc_orr_uit_pr"}:
                    return reply(code="E0002", message="칸 오류")  # 수량·호가유형 칸은 없다
                row = ledger.orders.get(str(body["org_orr_no"]))
                if row is None or row["state"] != "open":
                    return reply(code="E0003", message="정정 불가")
                ledger.amend(str(body["org_orr_no"]), body["fc_orr_uit_pr"])
                return reply({"orr_no": 5000 + int(body["org_orr_no"])})
            if path == NH_ORDER + "cancel":
                row = ledger.orders.get(str(body["org_orr_no"]))
                if row is None or row["state"] != "open":
                    return reply(code="E0004", message="취소 불가")
                ledger.cancel(str(body["org_orr_no"]))
                return reply({"orr_no": 6000 + int(body["org_orr_no"])})
            if path == "/gbstock/inquiry/v1/unexecuted":
                rows = [
                    {"orr_no": int(k), "iem_cd": v["symbol"], "fc_sec_trd_nat_cd": "200",
                     "oss_sby_dit_cd": "2" if v["side"] == "buy" else "1", "ahi_nmn_pr_tp_cd": v.get("ahi", "00"),
                     "orr_qty": v["qty"], "fc_orr_uit_pr": str(v["price"]), "cns_qty": v["filled"], "cns_pr": "0",
                     "ny_cns_orr_qty": v["qty"] - v["filled"]}
                    for k, v in ledger.open_rows().items()
                ]
                return reply(rows)
            raise AssertionError(path)

    broker = NH(
        account="12345678901",
        client_options={"app_key": "k", "app_secret": "s", "token": "t", "acct_type": "03"},
        client_factory=Server,
        allow_live_orders=True,
        clock=lambda: datetime(2026, 10, 27, 1, 0, tzinfo=UTC),
        rate_limiter=RateLimiter({"read": 10000}),
        safety=money(live_safety),
        market="US",
        venue="US",
        virtual=True,
    )
    return broker, calls, ledger


def nh_order(**changes):
    return request_order(venue=None, **changes)


def test_nh_overnight_walk_place_partial_amend_cancel_expire(live_safety):
    broker, calls, ledger = nh_fixture(live_safety)
    assert NH_US_DAYTIME_LIMIT == "63"
    placed = broker.place_order(nh_order())
    assert [c[0] for c in calls] == [NH_ORDER + "buy"]  # 송신 1회
    assert calls[0][1]["ahi_nmn_pr_tp_cd"] == "63" and calls[0][1]["fc_orr_uit_pr"] == 190.5
    oid = placed.id.removeprefix("us:")
    ledger.fill(oid, 1)
    seen = broker.status(placed)
    assert (seen.status, seen.filled, seen.remaining, seen.order_type) == ("partial", 1, 2, "limit")
    broker.amend_order(AmendRequest(order_id=placed.id, symbol="AAPL", side="buy", quantity=2, price="191",
                                    market="US", session="overnight", client_key="k-amend"))
    assert calls[-1][0] == NH_ORDER + "modify" and ledger.orders[oid]["price"] == Decimal("191")
    broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=2, market="US", session="overnight",
                                      client_key="k-cancel"))
    assert calls[-1][0] == NH_ORDER + "cancel" and ledger.orders[oid]["state"] == "cancelled"


def test_nh_expiry_cancel_after_vendor_auto_cancel_is_rejected_by_the_venue_only_once(live_safety):
    broker, calls, ledger = nh_fixture(live_safety)
    placed = broker.place_order(nh_order(side="sell", client_key="k-s"))
    assert calls[0][0] == NH_ORDER + "sell" and calls[0][1]["ahi_nmn_pr_tp_cd"] == "63"
    ledger.expire_all()
    # 원장 대사가 먼저: 열린 주문이 없으면 취소를 보내지 않는다(호출 쪽 책임) — 그래도 보내면 증권사가 거절하고 재시도 0.
    assert broker.list_orders("AAPL") == []
    before = len(calls)
    with pytest.raises(InvalidResponseError):
        broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=3, market="US", session="overnight",
                                          client_key="k-c"))
    assert len(calls) == before + 1


def test_nh_regular_request_keeps_code_00(live_safety):
    broker, calls, _ = nh_fixture(live_safety)
    broker.place_order(nh_order(session="regular", client_key="k-r"))
    assert calls[0][1]["ahi_nmn_pr_tp_cd"] == "00"


def test_nh_kr_never_takes_overnight(live_safety):
    broker = NH(account="12345678901", client_options={"app_key": "k", "app_secret": "s", "token": "t", "acct_type": "01"},
                client_factory=lambda base_url: SimpleNamespace(base_url=base_url), allow_live_orders=True,
                safety=money(live_safety))
    assert broker.capabilities().sessions == ("regular",)
    with pytest.raises(NotSupportedError):
        broker.place_order(request_order(venue=None))  # market US on a KR adapter


# --------------------------------------------------------------------------- 토스 계약 모의

class TossOvernightSdk(ContractMockTossSdk):
    def __init__(self, **options):
        super().__init__(**options)
        self.ledger = TossOvernightSdk.shared
        self.writes: list = []

    async def submit_order(self, **kwargs):
        result = await super().submit_order(**kwargs)
        payload = self.accepted[-1]
        order_id = self.ledger.place(payload["symbol"], payload["side"].lower(), int(payload["quantity"]), payload["price"])
        self.ledger.orders[order_id]["vendor_id"] = result["order_id"]
        self.ledger.orders[result["order_id"]] = self.ledger.orders[order_id]
        self.writes.append(("submit", payload))
        return result

    async def list_open_orders(self, symbol=None):
        rows, seen = [], set()
        for key, row in self.ledger.orders.items():
            if id(row) in seen or row["state"] != "open":
                continue
            seen.add(id(row))
            rows.append({"orderId": row["vendor_id"], "symbol": row["symbol"], "quantity": str(row["qty"]),
                         "execution": {"filledQuantity": str(row["filled"])},
                         "status": "PARTIAL_FILLED" if row["filled"] else "OPEN"})
        return {"hasNext": False, "orders": rows}

    async def modify_order(self, order_id, price):
        self.writes.append(("modify", order_id, price))
        row = self.ledger.orders.get(order_id)
        if row is None or row["state"] != "open":
            raise TossRejectedError("order-not-found")
        self.ledger.amend(order_id, price)  # US modify: 가격만 — quantity 칸이 아예 없다
        return {"submitted": True, "status": "MODIFY_SUBMITTED", "order_id": order_id}

    async def cancel_order(self, order_id):
        self.writes.append(("cancel", order_id))
        row = self.ledger.orders.get(order_id)
        if row is None or row["state"] != "open":
            raise TossRejectedError("order-not-cancellable")
        row["state"] = "cancelled"
        return {"status": "CANCEL_SUBMITTED", "order_id": order_id}


def toss_fixture(live_safety):
    TossOvernightSdk.shared = VenueLedger()
    sdk_holder = {}

    def factory(**options):
        sdk_holder["sdk"] = TossOvernightSdk(**options)
        return sdk_holder["sdk"]

    broker = Toss(account="fixture", account_seq=1, sdk_factory=factory, safety=money(live_safety), market="US",
                  allow_us_pilot=True, sdk_dry_run=False)
    return broker, sdk_holder, TossOvernightSdk.shared


def test_toss_overnight_walk_place_partial_amend_cancel_expire(live_safety):
    broker, holder, ledger = toss_fixture(live_safety)
    assert broker.capabilities().sessions == ("regular", "overnight") and broker.capabilities().supports_amend
    placed = broker.place_order(request_order(venue=None))
    sdk = holder["sdk"]
    submitted = sdk.accepted[0]
    assert "session" not in submitted and submitted["orderType"] == "LIMIT" and submitted["timeInForce"] == "DAY"
    assert [w[0] for w in sdk.writes] == ["submit"]  # 송신 1회
    ledger.fill(placed.id, 1)
    broker.amend_order(AmendRequest(order_id=placed.id, symbol="AAPL", side="buy", quantity=2, price="191.25",
                                    market="US", session="overnight", client_key="k-amend"))
    assert sdk.writes[-1] == ("modify", placed.id, "191.25")  # 가격만
    broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=2, market="US", session="overnight",
                                      client_key="k-cancel"))
    assert sdk.writes[-1] == ("cancel", placed.id) and ledger.orders[placed.id]["state"] == "cancelled"


def test_toss_expiry_then_cancel_or_amend_sends_nothing(live_safety):
    broker, holder, ledger = toss_fixture(live_safety)
    placed = broker.place_order(request_order(venue=None, client_key="k-e"))
    ledger.expire_all()
    sdk = holder["sdk"]
    writes = len(sdk.writes)
    for call in (
        lambda: broker.cancel_order(CancelRequest(order_id=placed.id, symbol="AAPL", quantity=3, market="US",
                                                  session="overnight", client_key="k-c")),
        lambda: broker.amend_order(AmendRequest(order_id=placed.id, symbol="AAPL", side="buy", quantity=3, price="192",
                                                market="US", session="overnight", client_key="k-a")),
    ):
        with pytest.raises(InvalidResponseError):  # 열린 주문 없음 — 송신 0
            call()
    assert len(sdk.writes) == writes


def test_toss_amend_with_wrong_quantity_sends_nothing(live_safety):
    broker, holder, _ = toss_fixture(live_safety)
    placed = broker.place_order(request_order(venue=None, client_key="k-q"))
    writes = len(holder["sdk"].writes)
    with pytest.raises(ConfigurationError):
        broker.amend_order(AmendRequest(order_id=placed.id, symbol="AAPL", side="buy", quantity=2, price="192",
                                        market="US", session="overnight", client_key="k-q2"))
    assert len(holder["sdk"].writes) == writes


def test_toss_kr_and_amend_without_declaration_are_refused(live_safety):
    kr = Toss(account="fixture", account_seq=1, sdk_factory=TossOvernightSdk, safety=money(live_safety), market="KR",
              sdk_dry_run=False)
    assert kr.capabilities().sessions == ("regular",)
    with pytest.raises(NotSupportedError):
        kr._validate_order(CancelRequest(order_id="o", symbol="005930", quantity=1, market="KR", session="overnight",
                                         client_key="k"))
