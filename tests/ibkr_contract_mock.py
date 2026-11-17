"""IBKR Client Portal contract mock (httpx.MockTransport, no network).

Answers from ``tests/fixtures/ibkr/openapi-options-subset.json`` (IB REST API 2.41.0): a request that breaks the
spec (unknown path/method/param, missing required param, enum or limit violation) gets the documented 400
``BadRequest`` shape; a valid one gets the spec's own example. Where the spec has no example the body is the
schema's minimum (string "", number 0, first enum value) — those paths are the ``_schema_only`` tests.
Scenario inputs a test chooses (market data values, option info rows) are passed in explicitly and never
masquerade as spec examples.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx

_SPEC = json.loads((Path(__file__).parent / "fixtures" / "ibkr" / "openapi-options-subset.json").read_text("utf-8"))
SPEC_PATHS = _SPEC["paths"]
_RESP = _SPEC["components"]["responses"]


def _ref(node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        cur: Any = _SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        node = cur
    return node


def schema_min(schema: Any) -> Any:
    """Smallest value of the declared type (enum -> first value)."""
    schema = _ref(schema)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type") or ("object" if "properties" in schema else None)
    if kind == "object":
        return {k: schema_min(v) for k, v in schema.get("properties", {}).items()}
    return {"string": "", "number": 0, "integer": 0, "boolean": False, "array": []}.get(kind)


def _first_example(response: dict) -> Any:
    media = _ref(response)["content"]["application/json"]
    return next(iter(media["examples"].values()))["value"]


def error_body(status: int) -> dict:
    return _first_example({401: _RESP["Unauthorized"], 400: _RESP["BadRequest"]}[status])


class IbkrContractMock:
    def __init__(
        self,
        *,
        strip_opt_sections: bool = False,
        strikes_empty: bool = False,
        info_rows: list[dict] | None = None,  # None: schema-minimum row echoing the request's right/strike
        market_data: dict[int, dict[str, str]] | None = None,  # scenario values per conid, field number -> text
        availability: str | None = None,  # None: the spec example for field 6509
        empty_first: int = 0,  # snapshot answers this many calls with only conid/conidEx (pre-flight)
        fail: dict[str, int | Exception] | None = None,  # path suffix -> HTTP status or exception
    ) -> None:
        self.opts = dict(strip_opt_sections=strip_opt_sections, strikes_empty=strikes_empty, info_rows=info_rows)
        self.market_data, self.availability, self.empty_first = market_data or {}, availability, empty_first
        self.fail = fail or {}
        self.calls: list[httpx.Request] = []
        self.snapshot_calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _bad(self, why: str) -> httpx.Response:
        body = dict(error_body(400), error=f"Bad Request: {why}")
        return httpx.Response(400, json=body)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        for suffix, failure in self.fail.items():
            if path.endswith(suffix):
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(failure, json=error_body(failure) if failure in (400, 401) else {})
        op = SPEC_PATHS.get(path, {}).get(request.method.lower())
        if op is None:
            return self._bad(f"{request.method} {path} is not in the OpenAPI subset")
        query = dict(request.url.params)
        specs = {p["name"]: p for p in op.get("parameters", [])}
        for name in query:
            if name not in specs:
                return self._bad(f"unknown param {name}")
        for name, p in specs.items():
            schema = _ref(p["schema"])
            if p.get("required") and name not in query:
                return self._bad(f"Missing required param: {name}")
            if name in query and "enum" in schema and query[name] not in map(str, schema["enum"]):
                return self._bad(f"{name} must be one of {schema['enum']}")
        return getattr(self, "_" + path.rsplit("/", 1)[1])(request, query, op)

    # -- endpoints ---------------------------------------------------------------------------------------
    def _search(self, request, query, op):
        rows = json.loads(json.dumps(_first_example(op["responses"]["200"])))
        if self.opts["strip_opt_sections"]:
            for row in rows:
                row["sections"] = [s for s in row.get("sections", []) if s.get("secType") != "OPT"]
        return httpx.Response(200, json=rows)

    def _strikes(self, request, query, op):
        body = _first_example(op["responses"]["200"])
        return httpx.Response(200, json={"call": [], "put": []} if self.opts["strikes_empty"] else body)

    def _info(self, request, query, op):
        if self.opts["info_rows"] is not None:
            return httpx.Response(200, json=self.opts["info_rows"])
        row = schema_min(_SPEC["components"]["schemas"]["secDefInfoResponse"])
        row.update(right=query.get("right", row["right"]), strike=float(query.get("strike", 0)))
        return httpx.Response(200, json=row)

    def _snapshot(self, request, query, op):
        conids, fields = query["conids"].split(","), query.get("fields", "").split(",")
        if len(conids) > 100 or len(fields) > 50:
            return self._bad("max 100 conids / 50 fields")
        if not all(c.isdigit() for c in conids):
            return self._bad("conids must be numeric")
        self.snapshot_calls += 1
        example = _ref(_SPEC["components"]["schemas"]["iserverSnapshot"])["items"]["properties"]
        rows = []
        for c in conids:
            row: dict[str, Any] = {"conid": int(c), "conidEx": f"{c}"}
            if self.snapshot_calls > self.empty_first:
                row.update({"_updated": example["_updated"]["example"], "6119": "q0", "server_id": "q0"})
                row["6509"] = self.availability or example["6509"]["example"]
                row.update({k: v for k, v in self.market_data.get(int(c), {}).items() if k in fields})
            rows.append(row)
        return httpx.Response(200, json=rows)

    def _history(self, request, query, op):
        if not re.fullmatch(r"\d+(min|h|d|w|m|y)", query["period"]) or not re.fullmatch(r"\d+(S|min|h|d|w|m)", query["bar"]):
            return self._bad("period/bar must be <n><unit> from the OpenAPI unit lists")
        examples = op["responses"]["200"]["content"]["application/json"]["examples"]
        key = {"Last": "iserverHistoryLastSuccessExample", "Bid_Ask": "iserverHistoryBidAskSuccessExample",
               "Midpoint": "iserverHistoryMidpointSuccessExample"}.get(query.get("source", "Last"))  # fmt: skip
        if key is None:
            return self._bad("source must be Last, Bid_Ask or Midpoint")
        return httpx.Response(200, json=examples[key]["value"])

    def _unsubscribeall(self, request, query, op):
        return httpx.Response(200, json=_first_example(op["responses"]["200"]))
