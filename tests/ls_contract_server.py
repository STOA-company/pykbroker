"""Stateful offline LS HTTP boundary, checked against pinned public TR fields.

Responses and account/order/token values are fabricated, never broker recordings.
No sockets are opened: tests install this at the existing urlopen boundary.
Error codes are deliberately synthetic because the public TR guide has no code table.
"""

import json
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).parent / "fixtures" / "ls" / "contract-20261024"


def contract(tr):
    return json.loads((FIXTURES / (tr + ".json")).read_text())


class Reply:
    def __init__(self, body):
        self.body = body
        self.headers = {"tr_cont": "N", "tr_cont_key": ""}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


class ContractServer:
    """Same post/headers/body contract as existing fakes, at the native HTTP seam."""

    def __init__(self):
        self.now = datetime(2026, 10, 24, 1, tzinfo=UTC)
        self.calls = []
        self.tokens = {}
        self.orders = {}
        self.next_order = 100
        self.token_ttl = 120
        self.ttl_field = "expire_in"
        self.next_response = None
        self.next_http_status = None
        self.after_write_failure = False
        self.enforce_rates = False
        self.windows = defaultdict(list)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def clock(self):
        return self.now

    def monotonic(self):
        return self.now.timestamp()

    def urlopen(self, request, *, timeout):
        assert timeout == 10.0
        url = urlsplit(request.full_url)
        assert url.scheme == "https" and url.netloc == "openapi.ls-sec.co.kr:8080"
        assert request.method == "POST"
        headers = {k.lower(): v for k, v in request.header_items()}
        if url.path == "/oauth2/token":
            body = {k: v[0] for k, v in parse_qs(request.data.decode()).items()}
            tr = "token"
        else:
            body = json.loads(request.data)
            tr = headers["tr_cd"]
        self.calls.append((url.path, tr, deepcopy(headers), deepcopy(body)))
        doc = contract(tr)
        assert url.path == doc["path"]
        assert headers["content-type"].split(";")[0] == (
            "application/x-www-form-urlencoded" if tr == "token" else "application/json")
        expected = doc["transaction"]["request"]["body"]
        assert set(body) == {field["element"] for field in expected}
        for field in expected:
            value = body[field["element"]]
            if field["type"] == "Object":
                assert set(value) == {f["element"] for f in field["fields"]}
                for item in field["fields"]:
                    self._type(value[item["element"]], item)
            else:
                self._type(value, field)
        if self.next_http_status is not None:
            status, self.next_http_status = self.next_http_status, None
            raise HTTPError(request.full_url, status, "fabricated", {}, BytesIO(b"{}"))
        if self.next_response is not None:
            response, self.next_response = self.next_response, None
            return Reply(response)
        if tr == "token":
            assert body == {"grant_type": "client_credentials", "appkey": "fixture-app-key",
                            "appsecretkey": "fixture-app-secret", "scope": "oob"}
            token = "fixture-token-" + str(len(self.tokens) + 1)
            self.tokens[token] = self.now + timedelta(seconds=self.token_ttl)
            return Reply({"access_token": token, "token_type": "Bearer", "scope": "oob",
                          self.ttl_field: str(self.token_ttl)})
        assert headers["tr_cont"] == "N" and headers["tr_cont_key"] == ""
        token = headers["authorization"].removeprefix("Bearer ")
        if token not in self.tokens or self.now >= self.tokens[token]:
            # NOT an LS code: confirms that an unrecognized auth failure cannot become success.
            return Reply({"rsp_cd": "SYNTH_AUTH", "rsp_msg": "fabricated"})
        if self.enforce_rates:
            window = self.windows[tr]
            window[:] = [time for time in window if (self.now - time).total_seconds() < 1]
            rate = int(doc["transaction"]["transactionPerSec"])
            if len(window) >= rate:
                return Reply({"rsp_cd": "SYNTH_RATE", "rsp_msg": "fabricated"})
            window.append(self.now)
        if tr == "CSPAQ12200":
            return Reply({"rsp_cd": "00000", "CSPAQ12200OutBlock1": {"AcntNo": "fixture-account"},
                          "CSPAQ12200OutBlock2": {"MnyOrdAbleAmt": 900000, "BalEvalAmt": 30000}})
        if tr == "t0424":
            return Reply({"rsp_cd": "00000", "t0424OutBlock": {"cts_expcode": ""},
                          "t0424OutBlock1": [{"expcode": "005930", "hname": "fabricated-stock",
                                              "janqty": 3, "mdposqt": 2, "pamt": 9900,
                                              "price": 10000, "appamt": 30000}]})
        if tr == "t0425":
            return Reply({"rsp_cd": "00000", "t0425OutBlock": {"cts_ordno": ""},
                          "t0425OutBlock1": list(deepcopy(self.orders).values())})
        row = next(iter(body.values()))
        assert row["IsuNo"] == "005930"  # The native real-account code path, without a broker connection.
        self.next_order += 1
        number = self.next_order
        if tr == "CSPAT00601":
            assert row["BnsTpCode"] in {"1", "2"}
            assert row["OrdprcPtnCode"] in {"00", "03"}
            assert row["MgntrnCode"] == "000" and row["LoanDt"] == ""
            assert row["OrdCndiTpCode"] == "0" and row["MbrNo"] == "KRX"
            assert (row["OrdPrc"] == 0) == (row["OrdprcPtnCode"] == "03")
            self.orders[str(number)] = {"ordno": number, "expcode": "005930", "qty": row["OrdQty"],
                                        "price": row["OrdPrc"], "cheqty": 0, "cheprice": 0,
                                        "ordrem": row["OrdQty"]}
        else:
            original = self.orders[str(row["OrgOrdNo"])]
            assert row["OrdQty"] <= original["ordrem"]
            original["ordrem"] -= row["OrdQty"]
            if tr == "CSPAT00701":
                assert row["OrdprcPtnCode"] in {"00", "03"} and row["OrdCndiTpCode"] == "0"
                self.orders[str(number)] = {"ordno": number, "expcode": "005930", "qty": row["OrdQty"],
                                            "price": row["OrdPrc"], "cheqty": 0, "cheprice": 0,
                                            "ordrem": row["OrdQty"]}
            else:
                assert tr == "CSPAT00801"
        if self.after_write_failure:
            self.after_write_failure = False
            raise TimeoutError("fabricated connection loss after acceptance")
        return Reply({"rsp_cd": "00000", tr + "OutBlock2": {"OrdNo": number}})

    @staticmethod
    def _type(value, field):
        if field["type"] == "String":
            assert isinstance(value, str) and len(value) <= int(field["length"])
        elif field["type"] == "Number":
            assert type(value) in (int, float)
        else:
            raise AssertionError("Unsupported contract field")

    def fill(self, order_id, quantity):
        row = self.orders[order_id]
        assert 0 < quantity <= row["ordrem"]
        row["cheqty"] += quantity
        row["ordrem"] -= quantity
        row["cheprice"] = 9950
