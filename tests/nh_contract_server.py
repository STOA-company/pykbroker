"""Stateful NH PLUG KR cash-order server built from the official OpenAPI snapshot (no network, no socket).

Requests are checked against tests/fixtures/nh/official/krstock-1024.json (required fields, types, the code
enums written in the field descriptions); answers are checked against the same file's response schemas.
Behaviours the spec does not state are simple and listed in docs/nh-spec.md: where an error code sits in the
envelope, which fills a ledger shows, and the mkt_orr_no <-> itg_orr_no link (one itg number per order chain
here; the adapter never infers it).
"""

import json
import re
from pathlib import Path

from tests.test_nh_official_contract import check_fields

SCHEMA = json.loads((Path(__file__).parent / "fixtures/nh/official/krstock-1024.json").read_text())
ORDER = "/krstock/order/v1/"
DAILY = "/krstock/inquiry/v1/dailyOrderExecution"
BALANCE = "/krstock/inquiry/v1/balance"
SELLABLE = "/krstock/inquiry/v1/sellableQuantity"


def _resolve(value):
    if isinstance(value, dict):
        if "$ref" in value:
            return _resolve(SCHEMA["components"]["schemas"][value["$ref"].split("/")[-1]])
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def _schema(path, kind):
    op = SCHEMA["paths"][path]["post"]
    if kind == "request":
        return _resolve(op["requestBody"]["content"]["application/json"]["schema"])
    return _resolve(op["responses"]["200"]["content"]["application/json"]["schema"])


def description_codes(path, field):
    """«01.보통가 05.시장가 ...» -> {code: name} from the official field description."""
    text = _schema(path, "request")["properties"]["Input_0"]["properties"][field]["description"]
    return dict(re.findall(r"(?<![0-9A-Za-z])([0-9]{1,2})\.([^\s0-9][^\s]*)", text.split("/")[-1]))


class NHContractServer:
    PRICED = {"01", "06"}  # 보통가, 조건부지정가 carry orr_pr; 16 스톱지정가 is not offered by the adapter

    def __init__(self):
        self.calls = []
        self.chains = {}  # itg number -> chain
        self.mkt = {}  # mkt_orr_no -> itg number
        self.next_mkt = 100
        self.cash, self.held, self.last = 1_000_000, 0, 990
        self.token_ok = True
        self.write_limit = None  # writes allowed before IGW42903
        self.lose_next_write = False  # the write lands, its answer is lost
        self.status_next_write = None  # an HTTP status answered instead of 200 (after the write landed)
        self.error_for = {}  # path -> code answered once, order NOT created
        self.writes = 0
        self.placed = []  # every accepted cashBuy/cashSell Input_0, in order

    # --- test controls ---
    def fill(self, mkt_no, qty, price):
        chain = self.chains[self.mkt[mkt_no]]
        assert 0 < qty <= chain["remaining"]
        chain["filled"] += qty
        chain["remaining"] -= qty
        chain["cost"] += qty * price
        sign = 1 if chain["side"] == "buy" else -1
        self.cash -= sign * qty * price
        self.held += sign * qty

    def live(self, mkt_no):
        return self.chains[self.mkt[mkt_no]]

    # --- boundary (the adapter's transport seam) ---
    def factory(self, *, base_url):
        server = self

        class Client:
            def __init__(self):
                self.base_url = base_url

            def post(self, path, *, headers, data):
                return server.post(path, headers, data)

        return Client()

    def _answer(self, path, output=None, code="00000", message="정상처리되었습니다"):
        body = {"message": {"msg_code": code, "usr_msg": message}}
        if output is not None:
            body["Output_0"] = output
        return 200, body, {}

    def post(self, path, headers, data):
        body = json.loads(data)
        self.calls.append((path, dict(headers), body))
        check_fields(_schema(path, "request"), body)
        row = body["Input_0"]
        is_write = path.startswith(ORDER)
        if is_write:
            self.writes += 1
            if path in self.error_for:
                return self._answer(path, {}, code=self.error_for.pop(path), message="")
            if not self.token_ok:
                return self._answer(path, {}, code="IGW40044", message="")
            if self.write_limit is not None and self.writes > self.write_limit:
                return self._answer(path, {}, code="IGW42903", message="")
        if path in (ORDER + "cashBuy", ORDER + "cashSell"):
            out = self._place(path, row)
        elif path == ORDER + "modify":
            out = self._amend(row)
        elif path == ORDER + "cancel":
            out = self._cancel(row)
        elif path == DAILY:
            out = self._daily()
        elif path == BALANCE:
            return self._checked(path, {"dca": self.cash, "orr_pbl_amt4": self.cash,
                                        "tot_eal_amt": self.held * self.last},
                                 extra={"Output_1": [{"iem_cd": "005930", "itg_bnc_qty": self.held, "phs_pr": self.last,
                                                      "now_pr": self.last, "eal_amt": self.held * self.last}]
                                        if self.held else []})
        elif path == SELLABLE:
            return self._checked(path, {"iem_cd": "005930", "sll_pbl_qty": self.held})
        else:
            raise AssertionError(f"unexpected contract path {path}")
        if path == DAILY:
            return self._checked(path, out)
        if self.lose_next_write:
            self.lose_next_write = False
            raise TimeoutError("fabricated connection loss after acceptance")
        if self.status_next_write is not None:
            status, self.status_next_write = self.status_next_write, None
            return status, {}, {}
        return self._checked(path, out)

    def _checked(self, path, output, extra=None):
        status, body, headers = self._answer(path, output)
        body.update(extra or {})
        check_fields(_schema(path, "response"), body)
        return status, body, headers

    def _next(self):
        self.next_mkt += 1
        return self.next_mkt

    def _place(self, path, row):
        assert row["nmn_pr_tp_cd"] in description_codes(path, "nmn_pr_tp_cd")
        assert row["orr_cnd_dit_cd"] in description_codes(path, "orr_cnd_dit_cd")
        assert row["rmt_mkt_cd"] in {"SOR", "KRX", "NXT"} and row["sor_mkt_sli_yn"] == "N"
        assert (("orr_pr" in row) == (row["nmn_pr_tp_cd"] in self.PRICED)), "price iff a priced kind"
        assert row["nmn_pr_tp_cd"] != "16"
        itg = 9000 + len(self.chains) + 1
        mkt = self._next()
        self.chains[itg] = {"side": "buy" if path.endswith("cashBuy") else "sell", "qty": row["orr_qty"],
                            "remaining": row["orr_qty"], "filled": 0, "cost": 0, "cancelled": 0, "corrected": 0,
                            "price": row.get("orr_pr", 0), "kind": row["nmn_pr_tp_cd"], "venue": row["rmt_mkt_cd"]}
        self.mkt[mkt] = itg
        self.placed.append(dict(row))
        return {"mkt_orr_no": mkt}

    def _amend(self, row):
        chain = self.live(row["org_mkt_orr_no"])
        assert row["all_pat_dit_cd"] == "2" and 0 < row["cor_qty"] <= chain["remaining"]
        assert row["rmt_mkt_cd"] == chain["venue"], "same market as the original order"
        chain["corrected"] += row["cor_qty"]
        chain["price"] = row["cor_pr"]
        mkt = self._next()
        self.mkt[mkt] = self.mkt[row["org_mkt_orr_no"]]
        return {"mkt_orr_no": mkt}

    def _cancel(self, row):
        chain = self.live(row["org_mkt_orr_no"])
        assert row["all_pat_dit_cd"] == "2" and 0 < row["cor_qty"] <= chain["remaining"]
        chain["remaining"] -= row["cor_qty"]
        chain["cancelled"] += row["cor_qty"]
        mkt = self._next()
        self.mkt[mkt] = self.mkt[row["org_mkt_orr_no"]]
        return {"mkt_orr_no": mkt}

    def _daily(self):
        names = description_codes(ORDER + "cashBuy", "nmn_pr_tp_cd")
        rows = []
        for itg, c in self.chains.items():
            rows.append({
                "itg_orr_no": itg, "iem_cd": "005930", "orr_qty": c["qty"], "orr_pr": c["price"],
                "tot_cns_qty": c["filled"], "ny_cns_qty": c["remaining"], "can_qty": c["cancelled"],
                "cns_avg_uit_pr": c["cost"] // c["filled"] if c["filled"] else 0,
                "sby_dit_cd_nm": "매수" if c["side"] == "buy" else "매도",
                "nmn_pr_tp_cd_nm": names.get(c["kind"], ""), "rmt_mkt_cd": c["venue"],
            })
        return rows
