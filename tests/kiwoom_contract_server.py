"""Contract fake of the Kiwoom REST server, built from the official spec (no network, no socket).

It speaks the same `post(path, *, headers, json) -> (body, headers)` boundary as the broker's real client, so the
broker runs its production code path against it. What it checks comes from
`tests/fixtures/kiwoom/official/request_schema.json` (extracted from the official kiwoom_api_spec.json, sha256 in
SOURCE.md): URL per api-id, required request fields, field lengths. Error codes and texts are the official table.

Stateful: orders, partial fills, cancels, cash and holdings move together, so a buy -> read -> partial fill ->
cancel -> balance walk is one consistent story. Behaviours the spec does NOT state are deliberately simple and listed
in docs/kiwoom-spec.md ("not confirmed"): where an error arrives (HTTP status vs body), the token lifetime, whether the
rate-limit bucket is per TR or per account, the exact ka10075/kt00007 row text after a cancel.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

OFFICIAL = Path(__file__).parent / "fixtures" / "kiwoom" / "official"
SCHEMA = json.loads((OFFICIAL / "request_schema.json").read_text())
ERRORS = json.loads((OFFICIAL / "error_codes_all.json").read_text())
KST_FMT = "%Y%m%d%H%M%S"
# kt10000 trde_tp list from the official spec (description column): code -> (label, needs ord_uv, tif, style)
TRDE = {
    "0": ("보통", True, "day", "limit"), "3": ("시장가", False, "day", "market"),
    "5": ("조건부지정가", True, "day", "limit"), "81": ("장마감후시간외", False, "day", "session"),
    "61": ("장시작전시간외", False, "day", "session"), "62": ("시간외단일가", False, "day", "session"),
    "6": ("최유리지정가", False, "day", "market"), "7": ("최우선지정가", False, "day", "market"),
    "10": ("보통(IOC)", True, "ioc", "limit"), "13": ("시장가(IOC)", False, "ioc", "market"),
    "16": ("최유리(IOC)", False, "ioc", "market"), "20": ("보통(FOK)", True, "fok", "limit"),
    "23": ("시장가(FOK)", False, "fok", "market"), "26": ("최유리(FOK)", False, "fok", "market"),
    "28": ("스톱지정가", True, "day", "limit"), "29": ("중간가", False, "day", "market"),
    "30": ("중간가(IOC)", False, "ioc", "market"), "31": ("중간가(FOK)", False, "fok", "market"),
}
VENUES = ("KRX", "NXT", "SOR")


def _n(value: int, width: int) -> str:
    return str(value).zfill(width)


class KiwoomContractServer:
    def __init__(self, *, now: datetime, cash: int = 1_000_000, price: int = 72_000, page_size: int = 50,
                 token_lifetime: timedelta = timedelta(hours=24)):
        self.now = now  # naive KST clock the server uses for expires_dt
        self.cash, self.price, self.page_size = cash, price, page_size
        self.token_lifetime = token_lifetime
        self.tokens: dict[str, datetime] = {}
        self.orders: dict[str, dict] = {}
        self.ledger_extra: list[dict] = []  # cancel-confirmation rows
        self.holdings: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.limits: dict[str, int] = {}  # api-id -> max calls per window; call tick() to open a new window
        self._window: dict[str, int] = {}
        self._seq = 0
        self.base_url = None
        self.liquidity = 10**9  # shares available to market-style / IOC / FOK orders at the quote
        self.invalid: list[tuple[str, int]] = []  # (api-id, 1511|1517) the broker should never provoke
        self.fail_next: dict[str, tuple[int, str]] = {}  # api-id -> body error to emit once

    # --- test controls ---
    def tick(self) -> None:
        self._window.clear()

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def fill(self, ord_no: str, qty: int, price: int | None = None) -> None:
        order = self.orders[ord_no]
        qty = min(qty, order["remaining"])
        price = price or order["price"]
        order["cntr_qty"] += qty
        order["remaining"] -= qty
        order["cntr_amt"] += qty * price
        sign = 1 if order["side"] == "buy" else -1
        self.cash -= sign * qty * price
        row = self.holdings.setdefault(order["code"], {"qty": 0, "cost": 0})
        row["qty"] += sign * qty
        row["cost"] += sign * qty * price

    # --- boundary ---
    def post(self, path: str, *, headers: dict, json: dict):  # noqa: A002 - mirrors the real client signature
        self.calls.append((path, dict(json)))
        if path == "/oauth2/token":
            return self._token(json)
        api_id = headers.get("api-id")
        if not api_id:
            return self._err(1501)
        if api_id not in SCHEMA:
            return self._err(1505, api_id=api_id)
        spec = SCHEMA[api_id]
        if spec["url"] != path:
            return self._err(1504, api_id=api_id, uri=path)
        auth = headers.get("authorization")
        if not auth:
            return self._err(1513)
        parts = auth.split(" ")
        if len(parts) != 2:
            return self._err(1514)
        if parts[0] != "Bearer":
            return self._err(1515)
        expires = self.tokens.get(parts[1])
        if expires is None or self.now >= expires:
            return self._err(8005)
        if self.limits.get(api_id) is not None:
            self._window[api_id] = self._window.get(api_id, 0) + 1
            if self._window[api_id] > self.limits[api_id]:
                return self._err(1700, api_id=api_id)
        bad = self._validate(spec, json)
        if bad:
            self.invalid.append((api_id, bad[0]["return_code"]))
            return bad
        if api_id in self.fail_next:
            code, _ = self.fail_next.pop(api_id)
            return self._err(code)
        handler = getattr(self, f"_{api_id}", None)
        if handler is None:
            return self._err(1999)
        return handler(json, headers)

    def _err(self, code: int, **fmt):
        text = ERRORS[str(code)]
        for key, value in fmt.items():
            text = text.replace("{?}", str(value), 1)
        return {"return_code": code, "return_msg": text}, {"cont-yn": "N"}

    def _validate(self, spec, body):
        missing = [k for k, f in spec["request"].items() if f["required"] and k not in body]
        if missing:
            return self._err(1511, p=",".join(missing))
        for key, value in body.items():
            field = spec["request"].get(key)
            if field is None or not isinstance(value, str):
                return self._err(1517, p=key)
            if field["length"] and len(value) > int(field["length"]):
                return self._err(1517, p=key)
        return None

    def _token(self, body):
        if not all(k in body for k in ("appkey", "secretkey")):
            return self._err(8020)
        if "grant_type" not in body:
            return self._err(8011)
        if body["grant_type"] != "client_credentials":
            return self._err(8012)
        self._seq += 1
        token = f"contract-token-{self._seq}"
        expires = self.now + self.token_lifetime
        self.tokens[token] = expires
        return {"expires_dt": expires.strftime(KST_FMT), "token_type": "bearer", "token": token, "return_code": 0,
                "return_msg": "정상적으로 처리되었습니다"}, {"cont-yn": "N"}

    @staticmethod
    def _ok(body, **headers):
        return {**body, "return_code": 0, "return_msg": "정상적으로 처리되었습니다"}, {"cont-yn": "N", **headers}

    # --- market data ---
    def _ka10001(self, body, _):
        return self._ok({"stk_cd": body["stk_cd"], "stk_nm": "삼성전자", "cur_prc": str(self.price),
                         "upl_pric": str(int(self.price * 1.3)), "lst_pric": str(int(self.price * 0.7)),
                         "base_pric": str(self.price)})

    def _ka10004(self, body, _):
        return self._ok({"buy_fpr_bid": str(self.price - 100), "sel_fpr_bid": str(self.price + 100)})

    # --- orders ---
    def _place(self, body, side):
        venue = body["dmst_stex_tp"]
        if venue not in VENUES:
            return self._err(1517, p="dmst_stex_tp")
        if body["trde_tp"] not in TRDE:
            return self._err(1517, p="trde_tp")
        label, needs_price, tif, style = TRDE[body["trde_tp"]]
        if needs_price and not int(body.get("ord_uv") or 0):
            return self._err(1517, p="ord_uv")
        qty = int(body["ord_qty"])
        price = int(body.get("ord_uv") or 0)
        if side == "buy" and qty * (price or self.price) > self.cash:
            return {"return_code": 20, "return_msg": "주문가능금액 부족"}, {"cont-yn": "N"}
        self._seq += 1
        ord_no = _n(self._seq, 7)
        order = {"side": side, "code": body["stk_cd"], "qty": qty, "price": price, "remaining": qty, "cntr_qty": 0,
                 "cntr_amt": 0, "cancelled": 0, "venue": venue, "label": label, "trde_tp": body["trde_tp"]}
        self.orders[ord_no] = order
        if style == "market" or tif in ("ioc", "fok"):
            can = min(qty, self.liquidity)
            if tif == "fok" and can < qty:
                can = 0
            if can:
                self.fill(ord_no, can, self.price)
            if tif in ("ioc", "fok"):
                order["cancelled"] += order["remaining"]  # the unfilled rest is killed at once
                order["remaining"] = 0
        return self._ok({"ord_no": ord_no, "dmst_stex_tp": venue})

    def _kt10000(self, body, _):
        return self._place(body, "buy")

    def _kt10001(self, body, _):
        return self._place(body, "sell")

    def _kt10003(self, body, _):
        order = self.orders.get(body["orig_ord_no"])
        if order is None or order["code"] != body["stk_cd"] or order["venue"] != body["dmst_stex_tp"]:
            return {"return_code": 20, "return_msg": "원주문번호 없음"}, {"cont-yn": "N"}
        qty = int(body["cncl_qty"]) or order["remaining"]  # spec: '0' cancels the whole remainder
        if qty > order["remaining"] or qty == 0:
            return {"return_code": 20, "return_msg": "취소가능수량 초과"}, {"cont-yn": "N"}
        order["remaining"] -= qty
        order["cancelled"] += qty
        self._seq += 1
        cancel_no = _n(self._seq, 7)
        self.ledger_extra.append({"ord_no": cancel_no, "ori_ord": body["orig_ord_no"], "stk_cd": body["stk_cd"],
                                  "qty": qty, "kind": "취소확인", "venue": order["venue"]})
        return self._ok({"ord_no": cancel_no, "base_orig_ord_no": body["orig_ord_no"], "cncl_qty": _n(qty, 12)})

    def _kt10002(self, body, _):
        order = self.orders.get(body["orig_ord_no"])
        if order is None or order["code"] != body["stk_cd"] or order["venue"] != body["dmst_stex_tp"]:
            return {"return_code": 20, "return_msg": "원주문번호 없음"}, {"cont-yn": "N"}
        qty = int(body["mdfy_qty"]) or order["remaining"]  # spec: '0' amends the whole remainder
        if qty > order["remaining"] or qty == 0 or not int(body["mdfy_uv"]):
            return {"return_code": 20, "return_msg": "정정가능수량 초과"}, {"cont-yn": "N"}
        order["remaining"] -= qty
        order["amended"] = order.get("amended", 0) + qty
        self._seq += 1
        new_no = _n(self._seq, 7)
        self.orders[new_no] = {**order, "qty": qty, "price": int(body["mdfy_uv"]), "remaining": qty, "cntr_qty": 0,
                               "cntr_amt": 0, "cancelled": 0, "amended": 0,
                               "root": order.get("root", body["orig_ord_no"]), "ori": body["orig_ord_no"],
                               "mdfy": "정정확인"}
        return self._ok({"ord_no": new_no, "base_orig_ord_no": order.get("root", body["orig_ord_no"]),
                         "mdfy_qty": _n(qty, 12), "dmst_stex_tp": order["venue"]})

    # --- reads (paged by cont-yn / next-key, as the spec describes) ---
    def _page(self, rows, headers):
        start = int(headers.get("next-key") or 0) if headers.get("cont-yn") == "Y" else 0
        chunk = rows[start:start + self.page_size]
        more = start + self.page_size < len(rows)
        return chunk, ({"cont-yn": "Y", "next-key": str(start + self.page_size)} if more else {"cont-yn": "N"})

    def _ka10075(self, body, headers):
        rows = [
            {"acnt_no": "0000000000", "ord_no": no, "stk_cd": o["code"], "stk_nm": "삼성전자", "ord_stt": "접수",
             "ord_qty": str(o["qty"]), "ord_pric": f"+{o['price']}", "oso_qty": str(o["remaining"]),
             "cntr_tot_amt": str(o["cntr_amt"]), "orig_ord_no": "0000000",
             "io_tp_nm": "+매수" if o["side"] == "buy" else "-매도", "trde_tp": o["label"], "tm": "093005",
             "cntr_qty": str(o["cntr_qty"]), "stex_tp": {"KRX": "1", "NXT": "2", "SOR": "0"}[o["venue"]],
             "stex_tp_txt": o["venue"]}
            for no, o in self.orders.items()
            if o["remaining"] > 0 and (body["all_stk_tp"] == "0" or o["code"] == body.get("stk_cd"))
            and body["stex_tp"] in ("0", {"KRX": "1", "NXT": "2", "SOR": "0"}[o["venue"]])
        ]
        chunk, hdr = self._page(rows, headers)
        return self._ok({"oso": chunk}, **hdr)

    def _kt00007(self, body, headers):
        rows = []
        want = body["dmst_stex_tp"]
        for no, o in self.orders.items():
            if want not in ("%", o["venue"]):
                continue
            rows.append({"ord_no": no, "stk_cd": "A" + o["code"], "trde_tp": o["label"], "ord_qty": _n(o["qty"], 10),
                         "ord_uv": _n(o["price"], 10), "cnfm_qty": "0000000000", "acpt_tp": "접수",
                         "ori_ord": o.get("ori", "0000000"), "stk_nm": "삼성전자",
                         "io_tp_nm": "현금매수" if o["side"] == "buy" else "현금매도",
                         "cntr_qty": _n(o["cntr_qty"], 10),
                         "cntr_uv": _n(o["cntr_amt"] // o["cntr_qty"], 10) if o["cntr_qty"] else "0000000000",
                         "ord_remnq": _n(o["remaining"], 10), "mdfy_cncl": o.get("mdfy", ""),
                         "dmst_stex_tp": o["venue"]})
        for extra in self.ledger_extra:
            if want not in ("%", extra["venue"]):
                continue
            rows.append({"ord_no": extra["ord_no"], "stk_cd": "A" + extra["stk_cd"], "trde_tp": "보통",
                         "ord_qty": _n(extra["qty"], 10), "ord_uv": "0000000000", "cnfm_qty": _n(extra["qty"], 10),
                         "acpt_tp": "확인", "ori_ord": extra["ori_ord"], "stk_nm": "삼성전자",
                         "io_tp_nm": "현금매수취소", "cntr_qty": "0000000000", "cntr_uv": "0000000000",
                         "ord_remnq": "0000000000", "mdfy_cncl": extra["kind"], "dmst_stex_tp": extra["venue"]})
        chunk, hdr = self._page(rows, headers)
        return self._ok({"acnt_ord_cntr_prps_dtl": chunk}, **hdr)

    def _kt00001(self, body, _):
        cash = _n(self.cash, 15)
        return self._ok({"entr": cash, "ord_alow_amt": cash, "100stk_ord_alow_amt": cash, "d2_entra": cash})

    def _kt00018(self, body, headers):
        rows = [
            {"stk_cd": "A" + code, "stk_nm": "삼성전자", "pur_pric": _n(h["cost"] // h["qty"], 15),
             "rmnd_qty": _n(h["qty"], 15), "trde_able_qty": _n(h["qty"], 15), "cur_prc": _n(self.price, 12),
             "evlt_amt": _n(h["qty"] * self.price, 15)}
            for code, h in self.holdings.items() if h["qty"] > 0
        ]
        chunk, hdr = self._page(rows, headers)
        return self._ok({"acnt_evlt_remn_indv_tot": chunk}, **hdr)
