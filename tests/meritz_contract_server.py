"""In-process fake server, pinned OpenAPI 3.1 request schema + synthetic ledger.

No network, keys or vendor sandbox. Numbers/prices are fixture scenario values;
API field names/types/limits/examples are read independently from official JSON.
"""

import copy
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/meritz/official"
SPEC = json.loads((ROOT / "openapi.json").read_text())


def validate(value, schema):
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict)
        assert set(schema.get("required", [])) <= value.keys()
        assert value.keys() <= schema["properties"].keys()
        for k, v in value.items():
            validate(v, schema["properties"][k])
    elif kind == "string":
        assert isinstance(value, str)
        assert len(value) <= schema.get("maxLength", len(value))
    elif kind == "number":
        assert not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
    elif kind == "array":
        assert isinstance(value, list)
        for item in value:
            validate(item, schema["items"])


class ContractServer:
    base_url = SPEC["servers"][0]["url"]

    def __init__(self):
        self.calls, self.orders = [], {"KR": {}, "US": {}}
        self.now, self.expiry, self.limit = 0, 604800, 10
        self.counts = Counter()
        self.failure = None
        self.write_failure = None
        self.warning = "0"
        self.lose_next_write = False
        self.next_id = 100
        self.page_size = 30
        self.holdings = {"KR": [], "US": []}
        self.cash = {"KR": Decimal(100000), "US": Decimal(10000)}
        self.overrides = {}
        self.today = {}

    def example(self, method, path):
        return copy.deepcopy(SPEC["paths"][path][method.lower()]["responses"]["200"]
                             ["content"]["application/json"]["example"])

    def request(self, method, path, *, headers, params=None, body=None, form=False):
        op = SPEC["paths"][path][method.lower()]
        if method == "POST":
            mime = "application/x-www-form-urlencoded" if form else "application/json"
            validate(body, op["requestBody"]["content"][mime]["schema"])
        query = {p["name"]: p for p in op["parameters"] if p["in"] == "query"}
        params = params or {}
        assert params.keys() <= query.keys()
        assert {k for k, p in query.items() if p["required"]} <= params.keys()
        for k, v in params.items():
            validate(v, query[k]["schema"])
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if path == "/oauth2/token":
            assert form and body["grant_type"] == "client_credentials" and body["scope"] == "login"
            return {"access_token": "fixture-token", "expires_in": "604800", "token_type": "Bearer"}
        assert headers["mac_address"] == "001122AABBCC"
        assert headers["Authorization"] == "Bearer fixture-token"
        if self.failure:
            code, self.failure = self.failure, None
            return {"rsp_cd": code}
        if self.now >= self.expiry:
            return {"rsp_cd": "EGW00123"}
        self.counts[(path, self.now)] += 1
        if self.counts[(path, self.now)] > self.limit:
            return {"rsp_cd": "EGW00200"}
        market = "US" if "/overseas/" in path else "KR"
        if path in self.overrides:
            return copy.deepcopy(self.overrides[path])
        if method == "POST":
            if self.write_failure:
                code, self.write_failure = self.write_failure, None
                return {"rsp_cd": code}
            if self.warning != "0":
                return {"rsp_cd": "0001", "data": {"oder_no": 0, "warn_cls_code": self.warning}}
            assert body.get("warn_cnfr_yn") == "N"
            action = path.rsplit("/", 1)[-1]
            if action in {"buy", "sell"}:
                row = self._new(market, action, body)
            else:
                original = self.orders[market][str(body["orgl_oder_no"])]
                if market == "KR":
                    assert original["rctf_cncl_cls_code"] == "1"
                row = self._change(market, action, body, original)
            self.orders[market][str(row["oder_no"])] = row
            result = {"rsp_cd": "0001", "data": {"oder_no": row["oder_no"], "warn_cls_code": "0"}}
            if self.lose_next_write:
                self.lose_next_write = False
                raise TimeoutError("synthetic lost acknowledgement")
            return result
        if path.endswith("/today"):
            return {"rsp_cd": "5766", "tr_cont": "0", "data": copy.deepcopy(list(self.today.values()))}
        if path.endswith("/history"):
            rows = list(self.orders[market].values())
            return self._page(rows, params)
        if path.endswith("/holdings"):
            return self._page(self.holdings[market], params)
        result = self.example(method, path)
        if path.endswith("/deposit"):
            result["data"]["oder_able_cash_amt"] = int(self.cash["KR"])
        if path.endswith("/estimate") and market == "US":
            result["data"][0]["oder_crnc_oder_able_camt"] = str(self.cash["US"])
        return result

    def _page(self, rows, params):
        start = 0
        if "tr_cont_key" in params:
            key = params["tr_cont_key"]
            assert params["tr_cont"] == "1" and key.endswith("  ")
            start = int(key[:-2])
        end = start + self.page_size
        more = end < len(rows)
        result = {"rsp_cd": "5762" if more else "5766", "tr_cont": "1" if more else "0",
                  "data": copy.deepcopy(rows[start:end])}
        if more:
            result["tr_cont_key"] = str(end) + "  "
        return result

    def _new(self, market, side, body):
        self.next_id += 1
        prefix = "/trading/v1/" + ("overseas/" if market == "US" else "") + "orders/history"
        row = self.example("GET", prefix)["data"][0]
        row["oder_no"] = self.next_id
        if market == "KR":
            assert body["iscd"].startswith("A") and len(body["iscd"]) == 7
            # openapi.json /trading/v1/orders/buy requestBody: oder_cls_code description.
            assert body["oder_cls_code"] in {"01", "05", "06", "12", "13", "61", "71", "83", "84"}
            assert body["exch_kind_code"] in {"01", "02", "03"}
            assert body["oder_cond_cls_code"] in {"0", "3", "4"}
            if body["oder_cls_code"] in {"05", "12", "13", "61", "71", "84"}:
                assert body["oder_unpr"] == 0
            if body["oder_cls_code"] == "83":
                assert body["aspr_cond_prc"] > 0
            row.update(iscd=body["iscd"], odqt=body["odqt"], ncnt_qty=body["odqt"], total_cntg_qty=0,
                       cntg_unpr=0, oder_unpr=body["oder_unpr"], oder_cls_code=body["oder_cls_code"],
                       deal_cls_code="2" if side == "buy" else "1", cncl_qty=0, cnfr_qty=0, rctf_qty=0,
                       orgl_oder_no=0, rctf_cncl_cls_code="1", oder_rfus_reas_code="0000")
        else:
            # openapi.json overseas holdings: ovrs_exch_vndr_idno description maps these codes.
            suffix, native_market = {"0537": ("OQ", "01"), "0321": ("NY", "01"), "0066": ("AX", "01"),
                                     "0104": ("HK", "02"), "0215": ("SH", "03"), "0214": ("SZ", "04"),
                                     "0106": ("JP", "05"), "0107": ("fixtureOSE", "05"),
                                     "0112": ("fixtureJAS", "05"), "0770": ("fixtureHOSE", "24"),
                                     "0771": ("fixtureHNX", "24")}[body["ovrs_exch_vndr_idno"]]
            assert body["oder_cls_code"] in {"01", "05", "31", "32", "33", "34"}
            assert body["oder_cond_cls_code"] == {"01": "0", "05": "0", "31": "2", "32": "7",
                                                  "33": "2", "34": "7"}[body["oder_cls_code"]]
            row.update(iscd=body["ovrs_stck_symb_iscd"] + "." + suffix, oder_dqty=str(body["odqt"]),
                       cntg_dqty="0", ncnt_odqt=0, ovrs_stck_oder_prc=body["ovrs_stck_oder_prc"],
                       cntg_crnc_prc="0", cntg_camt="0",
                       aspr_type_code={"01": "2", "05": "1", "31": "4", "32": "5", "33": "6", "34": "7"}[
                           body["oder_cls_code"]], ovrs_stck_mrkt_tr_cls_code=native_market,
                       seln_byov_clcd="2" if side == "buy" else "1", oder_date="20261025",
                       ovrs_stck_oder_stat_cls_code="0", oder_rctf_cncl_cls_code="0", orgl_oder_no=0)
            current = self.example("GET", "/trading/v1/overseas/orders/today")["data"][0]
            current.update(oder_date=row["oder_date"], oder_no=row["oder_no"], iscd=row["iscd"],
                           seln_byov_clcd=row["seln_byov_clcd"], odqt=body["odqt"], cntg_qty=0,
                           ovrs_exch_vndr_idno=body["ovrs_exch_vndr_idno"],
                           ncnt_qty=body["odqt"], oder_able_qty=body["odqt"],
                           cncl_cnfr_qty=0, rctf_cnfr_qty=0)
            self.today[str(row["oder_no"])] = current
        return row

    def _change(self, market, action, body, original):
        self.next_id += 1
        row = copy.deepcopy(original)
        row["oder_no"] = self.next_id
        row["orgl_oder_no"] = body["orgl_oder_no"]
        if market == "KR":
            assert body["iscd"] == original["iscd"]
            assert body["whol_rctf_cncl_yn"] == "N"
            ambiguous = action == "cancel" and any(
                r["rctf_cncl_cls_code"] == "2" and r["orgl_oder_no"] == original["oder_no"]
                for r in self.orders["KR"].values())
            if not ambiguous:
                assert body["odqt"] <= original["ncnt_qty"]
            if action == "modify":
                original["rctf_qty"] += body["odqt"]
                original["ncnt_qty"] -= body["odqt"]
                row.update(odqt=body["odqt"], total_cntg_qty=0, cntg_unpr=0, ncnt_qty=body["odqt"],
                           oder_unpr=body["oder_unpr"], rctf_cncl_cls_code="2", rctf_qty=0)
            else:
                # No documented allocation among original/amended prices: do not invent one.
                if not ambiguous:
                    original["ncnt_qty"] -= body["odqt"]
                    original["cncl_qty"] += body["odqt"]
                    original["cnfr_qty"] += body["odqt"]
                row.update(rctf_cncl_cls_code="3", odqt=body["odqt"], total_cntg_qty=0, ncnt_qty=0,
                           cncl_qty=body["odqt"], cnfr_qty=body["odqt"])
        else:
            assert body["oder_date"] == original["oder_date"]
            assert body["odqt"] <= Decimal(original["oder_dqty"]) - Decimal(original["cntg_dqty"])
            # Explicit synthetic today evidence in official fields, not a vendor execution claim.
            current = self.today[str(original["oder_no"])]
            current["rctf_cnfr_qty" if action == "modify" else "cncl_cnfr_qty"] += body["odqt"]
            current["ncnt_qty"] -= body["odqt"]
            current["oder_able_qty"] -= body["odqt"]
            changed = copy.deepcopy(current)
            changed.update(oder_no=row["oder_no"], orgl_oder_no=body["orgl_oder_no"],
                           odqt=body["odqt"], cntg_qty=0, rctf_cnfr_qty=0,
                           cncl_cnfr_qty=body["odqt"] if action == "cancel" else 0,
                           ncnt_qty=0 if action == "cancel" else body["odqt"],
                           oder_able_qty=0 if action == "cancel" else body["odqt"])
            self.today[str(row["oder_no"])] = changed
            row.update(oder_rctf_cncl_cls_code="1" if action == "modify" else "2",
                       oder_dqty=str(body["odqt"]), cntg_dqty="0", cntg_crnc_prc="0", cntg_camt="0")
            if action == "modify":
                row["ovrs_stck_oder_prc"] = body["ovrs_stck_oder_prc"]
        return row

    def fill(self, market, ident, quantity=1, price=1000):
        row = self.orders[market][ident]
        if market == "KR":
            row["total_cntg_qty"] += quantity
            row["ncnt_qty"] -= quantity
            row["cntg_unpr"] = price
        else:
            current = self.today[ident]
            current["cntg_qty"] += quantity
            current["ncnt_qty"] -= quantity
            current["oder_able_qty"] -= quantity
            row["cntg_dqty"] = str(Decimal(row["cntg_dqty"]) + quantity)
            row["cntg_crnc_prc"] = str(price)
            row["cntg_camt"] = str(Decimal(row["cntg_dqty"]) * price)
        sign = 1 if row.get("deal_cls_code", row.get("seln_byov_clcd")) == "2" else -1
        self.cash[market] -= sign * quantity * price
        path = "/accounts/v1/me/" + ("holdings" if market == "KR" else "overseas/holdings")
        holding = self.example("GET", path)["data"][0]
        if market == "KR":
            holding.update(iscd=row["iscd"], rmnd_qty=quantity, psnt_prc=str(price), vltn_amt=quantity * price)
        else:
            holding.update(iscd=row["iscd"], ovrs_stck_symb_iscd=row["iscd"].rsplit(".", 1)[0],
                           rmnd_dqty=str(quantity), seln_able_dqty=str(quantity), psnt_prc=str(price),
                           frcr_vltn_amt=str(quantity * price))
        self.holdings[market] = [holding]
