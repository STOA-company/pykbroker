"""Replay of the real Pykis call chain, driven by recorded fixtures.

A bare `Mock()` answers every attribute, so it hides the two places the vendor itself
refuses a mock (virtual) account. This double reproduces the vendor's own control flow,
line for line, so a test that passes here would also pass against the vendor:

* `_pykis/scope/account/api/revisable_order.py:16-17` raises `NotImplementedError`
  when `key.virtual_account` is true, and `base/domestic.py:101-114`
  (`get_open_orders_domestic`) is nothing but a call into `revisable_order_all`.
* `base/overseas.py:373-388` (`get_open_orders_overseas`) hardcodes the real-account
  TR `TTTS3018R` with no virtual branch, so the mock domain rejects the request.
* `_pykis/scope/account/api/daily_order.py:71` *does* switch to the mock TR
  (`VTTC8001R`), which makes the daily ledger the one order path a mock account has.

Fixture provenance is recorded in `tests/fixtures/NOTES.md`.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

VIRTUAL_REVISABLE_REFUSAL = "가상계좌는 주식 정정/취소 가능 조회 기능을 지원하지 않습니다."
# base/overseas.py:377 — one TR id, no virtual branch.
OVERSEAS_OPEN_ORDER_TR = "TTTS3018R"
# _pykis/scope/account/api/daily_order.py:11-15 — [real, real-old, virtual, virtual-old].
DAILY_ORDER_TR_IDS = ("TTTC8001R", "CTSC9115R", "VTTC8001R", "VTSC9115R")


class VendorRejected(Exception):
    """What the transport raises when the venue refuses the TR outright."""


class VendorAccountScope:
    """`KisAccountScope`: what `client.account(...)` returns."""

    def __init__(self, *, virtual: bool, fixtures: dict[str, Any]) -> None:
        self.key = SimpleNamespace(virtual_account=virtual)
        self._fixtures = fixtures
        self.tr_ids: list[str] = []
        # US mock ledger (official inquire_ccnl, VTTS3035R): pages of rows, set per test.
        self.us_ledger_pages: list[list[dict[str, Any]]] = [[]]
        # US mock cash (official inquire_present_balance, VTRP6504R) output2 rows, set per test.
        self.us_cash_rows: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []
        self.client = SimpleNamespace(request=self._request)
        self.account = SimpleNamespace(build_body=lambda body: {"CANO": "00000000", "ACNT_PRDT_CD": "01", **body})

    def _request(self, method: str, path: str, *, headers: dict[str, str], params: dict[str, str], response: Any):
        """`client.request(..., response=None)`: a requests.Response-shaped answer, mock domain routing."""
        tr_id = headers["tr_id"]
        self.requests.append({"method": method, "path": path, "tr_id": tr_id, "params": params, "headers": headers})
        if self.key.virtual_account and tr_id.startswith("T"):
            raise VendorRejected(f"{tr_id} is not routable on the mock domain")
        if path == "/uapi/overseas-stock/v1/trading/inquire-present-balance":
            assert tr_id == "VTRP6504R"
            body = {"rt_cd": "0", "output1": [], "output2": self.us_cash_rows}
            return SimpleNamespace(status_code=200, headers={"tr_cont": "D"}, json=lambda: body)
        assert path == "/uapi/overseas-stock/v1/trading/inquire-ccnl"
        assert response is None
        index = 0 if not params["CTX_AREA_NK200"] else int(params["CTX_AREA_NK200"])
        more = index + 1 < len(self.us_ledger_pages)
        body = {"rt_cd": "0", "output": self.us_ledger_pages[index],
                "ctx_area_nk200": str(index + 1) if more else "", "ctx_area_fk200": ""}
        return SimpleNamespace(status_code=200, headers={"tr_cont": "M" if more else "D"}, json=lambda: body)

    def overseas_cancel(self, market: str, code: str, order: Any, qty: int, **_: Any) -> Any:
        self.cancels.append({"tr_id": "VTTT1004U" if self.key.virtual_account else "TTTT1004U", "market": market,
                             "code": code, "odno": order.odno, "branch": order.krx_fwdg_ord_orgno, "qty": qty})
        return {"json_data": {"rt_cd": "0", "output": {"ODNO": "900"}}}

    # --- paths a mock account really has ---------------------------------

    def balance_all(self) -> Any:
        return self._fixtures["balance"]

    def amount(self, code: str | None = None) -> Any:
        return self._fixtures["amount"]

    def daily_order(
        self,
        start_date: datetime,
        end_date: datetime,
        *,
        code: str = "",
        ccld: str = "전체",
        old_order: bool = False,
        **_: Any,
    ) -> Any:
        # The vendor formats both dates and refuses a code that is not a six-digit symbol.
        if not isinstance(start_date, datetime) or not isinstance(end_date, datetime):
            raise TypeError("daily_order takes datetimes")
        start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")
        if code and len(code) != 6:
            raise ValueError("종목코드는 6자리입니다.")
        self.tr_ids.append(DAILY_ORDER_TR_IDS[(2 if self.key.virtual_account else 0) + (1 if old_order else 0)])
        rows = self._fixtures["daily_orders"]["output1"]
        if code:
            rows = [row for row in rows if row["pdno"] == code]
        if ccld == "미체결":
            rows = [row for row in rows if int(row["rmn_qty"]) > 0]
        return SimpleNamespace(orders=[SimpleNamespace(**row) for row in rows])

    def daily_order_all(self, **kwargs: Any) -> Any:
        return self.daily_order(**kwargs)

    # --- the path a mock account does not have ---------------------------

    def revisable_order(self, **_: Any) -> Any:
        if self.key.virtual_account:
            raise NotImplementedError(VIRTUAL_REVISABLE_REFUSAL)
        self.tr_ids.append("TTTC0084R")
        return self._fixtures["orders"]

    def revisable_order_all(self, **kwargs: Any) -> Any:
        return self.revisable_order(**kwargs)


class VendorDomestic:
    """`base.domestic`: the helper module pykbroker injects as `api`."""

    def __init__(self, fixtures: dict[str, Any]) -> None:
        self._fixtures = fixtures

    def get_price_domestic(self, kis_account: Any, stock_code: str) -> Any:
        return self._fixtures["quote"]

    def get_quote_domestic(self, kis_account: Any, stock_code: str) -> Any:
        return self._fixtures["book"]

    def get_open_orders_domestic(self, kis_account: Any, where_by: str = "전체") -> Any:
        # base/domestic.py:114 verbatim.
        return kis_account.revisable_order_all(where_by=where_by)

    def post_order_domestic(self, **_: Any) -> Any:
        return self._fixtures["submit"]


class VendorOverseas:
    """`base.overseas`: the US helper module, which has no virtual TR at all."""

    def __init__(self, fixtures: dict[str, Any]) -> None:
        self._fixtures = fixtures
        self.calls = 0

    def get_open_orders_overseas_all(self, kis_account: Any, market: str, **_: Any) -> Any:
        self.calls += 1
        if kis_account.key.virtual_account:
            # The mock domain has no route for the real-account TR the vendor hardcodes.
            raise VendorRejected(f"{OVERSEAS_OPEN_ORDER_TR} is not routable on the mock domain")
        return self._fixtures["open_orders_overseas"]


class VendorClient:
    """`PyKis`: holds the key that decides real vs. mock domain."""

    def __init__(self, *, virtual: bool, fixtures: dict[str, Any]) -> None:
        self.key = SimpleNamespace(virtual_account=virtual)
        self.scope = VendorAccountScope(virtual=virtual, fixtures=fixtures)

    def account(self, account: str) -> VendorAccountScope:
        return self.scope
