"""Saxo Bank OpenAPI adapter (REST, one broker class for stocks, ETFs and FX spot).

Sources: public Saxo OpenAPI reference + learn pages read 2026-10-27 (see tests/contract_walk/saxo_mock.py in
quantus-trader for the endpoint list). Every field the public docs do not settle is listed in the
trader repo's contract mock «UNVERIFIED» table and in SAXO_ASSUMPTIONS below; the S5 simulation walk
checks them against the real SIM gateway.

Rules this adapter keeps (SEAM §4):
  * An order write is sent once. 401/429 and a listed business ErrorCode are «not accepted»; a timeout, lost
    response, 202, 409, 5xx or an unlisted code is unknown and is never re-sent (reconcile by ExternalReference).
  * Saxo has no server-side duplicate guard (ExternalReference is not unique): the trader ledger claim in
    OrderSafety (client_key) is the only duplicate guard, exactly as for KIS/DBFI.
  * mode: paper = no Saxo call at all, demo = SIM gateway, live = production gateway. The caller (trader
    factory) decides the mode from the account row and applies its live gate to mode=live only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import (
    AmendRequest,
    Balance,
    CancelRequest,
    Capabilities,
    Order,
    OrderRequest,
    Position,
    Quote,
    Rules,
)
from pykbroker.reject_codes import DEFINITIVE_REJECTS, REJECTED
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal

SAXO_SIM_URL = "https://gateway.saxobank.com/sim/openapi"
SAXO_LIVE_URL = "https://gateway.saxobank.com/openapi"
SAXO_SIM_TOKEN_URL = "https://sim.logonvalidation.net/token"
SAXO_LIVE_TOKEN_URL = "https://live.logonvalidation.net/token"
_LOOPBACK = ("http://127.0.0.1:", "http://localhost:")

MODES = ("paper", "demo", "live")
EXTERNAL_REFERENCE_MAX = 50  # docs: «Max 50 characters; longer values are rejected»
_HASHED_PREFIX = "ck-"
_REFRESH_MARGIN_SEC = 60.0

#: Assumptions the docs do not settle (in addition to the 22 contract-mock UNVERIFIED cells).
SAXO_ASSUMPTIONS = (
    "StopLimit: OrderPrice = limit price, StopLimitPrice = stop trigger; Stop: OrderPrice = stop trigger",
    "OrderDuration.DurationType names: DayOrder GoodTillCancel GoodTillDate(+ExpirationDateTime) ImmediateOrCancel FillOrKill",
    "GET /port/v1/orders/me?Status=All may or may not list filled/cancelled orders (OrderStatusFilter values): a listed "
    "row decides; an absent order is decided by its last order activity, never by the absence",
    "202 with an OrderId and no ErrorInfo reads as accepted (the mock's 202 carries TradeNotCompleted ErrorInfo => unknown)",
    "Amend reads FilledAmount before the PATCH; a fill between that read and the PATCH grows the total by that fill",
    "PATCH Amount = new TOTAL order amount (filled + remaining); amend reads the order to learn FilledAmount",
    "PATCH carries the order's existing OrderDuration (row Duration) unless the request names one",
    "Cancel acknowledged + order absent from /port/v1/orders/me => NOT a proof (the contract mock drops filled and cancelled "
    "orders alike); the outcome is closed only by the order's last activity (/cs/v1/audit/orderactivities EntryType=Last)",
    "Order activity row: Status FinalFill/Cancelled/Expired with SubStatus «Confirmed» is final; FilledAmount is carried "
    "on the row (docs list Status/SubStatus/Amount in the example, not FilledAmount) — missing FilledAmount on a "
    "cancel keeps it unknown",
    "Token endpoint host sim.logonvalidation.net / live.logonvalidation.net (mock serves /token on the gateway base)",
    "Account row account_number holds the Saxo AccountKey (opaque), not AccountId",
    "Balance.position_value = TotalValue - CashBalance (clamped at 0)",
    "Short positions (negative PositionBase.Amount) are refused: pykbroker Position has no short side",
    "Instrument (Uic, AssetType) comes from the injected symbol_map; /ref/v1/instruments lookup is not built yet",
)


class SaxoInstrument(NamedTuple):
    uic: int
    asset_type: str
    market: str = "US"


@dataclass(frozen=True)
class SaxoTokenState:
    """What the owner persists after a refresh (Saxo rotates the refresh token on every use)."""

    token: str
    expires_at: float | None
    refresh_token: str | None


def external_reference(client_key: str) -> str:
    """client_key (<=128 chars) -> ExternalReference (<=50 chars), deterministic.

    A key that is short and does not start with the reserved prefix «ck-» is sent as is. Anything else becomes
    «ck-» + the first 40 hex digits of sha256(client_key) (43 chars, 160 bits): the same key always gives the same
    reference, so reconciliation recomputes it, and a short key can never equal a hashed one (reserved prefix).
    """
    if len(client_key) <= EXTERNAL_REFERENCE_MAX and not client_key.startswith(_HASHED_PREFIX):
        return client_key
    return _HASHED_PREFIX + hashlib.sha256(client_key.encode()).hexdigest()[:40]


_ORDER_TYPE = {"market": "Market", "limit": "Limit", "stop": "Stop", "stop_limit": "StopLimit"}
_ORDER_TYPE_BACK = {v: k for k, v in _ORDER_TYPE.items()}
_DURATION = {"day": "DayOrder", "gtc": "GoodTillCancel", "gtd": "GoodTillDate",
             "ioc": "ImmediateOrCancel", "fok": "FillOrKill"}
_OPEN = {"Working", "WorkingLockedCancelPending", "WorkingLockedChangePending", "NotWorking",
         "NotWorkingLockedCancelPending", "NotWorkingLockedChangePending", "LockedPlacementPending"}
_TIMEZONE = {"US": "America/New_York", "SG": "Asia/Singapore", "FX": "UTC"}
_CURRENCY = (("US", "USD"), ("SG", "SGD"))
_PAGE = 100
#: Final OrderLogStatus values (cs/v1/audit/orderactivities schema-orderlogstatus). Fill, Placed, Changed, Working and
#: DoneForDay are not final: the order may still fill.
_ACTIVITY_FINAL = {"FinalFill": "filled", "Cancelled": "cancelled", "Expired": "cancelled"}
_MAX_PAGES = 50


@dataclass
class _Reply:
    status: int
    body: Any
    headers: dict


class _NoReply(Exception):
    """No HTTP answer at all (timeout, connection reset, DNS)."""


class _Http:
    """One urllib call per request, never retried, never following a redirect."""

    def send(self, method: str, url: str, *, headers: dict, body: Any = None, form: dict | None = None,
             timeout: float) -> _Reply:
        data, hdrs = None, dict(headers)
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        request = Request(url, data=data, method=method, headers=hdrs)
        try:
            with urlopen(request, timeout=timeout) as response:
                status, raw, rh = response.status, response.read(), dict(response.headers)
        except HTTPError as exc:
            status, raw, rh = exc.code, exc.read(), dict(exc.headers)
            exc.close()
        except (URLError, OSError, ConnectionError):
            raise _NoReply from None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return _Reply(status, parsed, rh)


class SaxoOutcomeUnknownError(InvalidResponseError):
    """The write may or may not have been accepted. `order_id` is kept when Saxo named one (a 400 or 202 that
    carries an OrderId plus ErrorInfo means the main order exists and only a related order failed)."""

    def __init__(self, message: str, order_id: str | None = None) -> None:
        self.order_id = order_id
        super().__init__(message)


def _error_code(body: Any) -> str | None:
    """First ErrorInfo.ErrorCode in a body (a DELETE carries one per Orders[] item)."""
    if not isinstance(body, Mapping):
        return None
    info = body.get("ErrorInfo")
    if info is None:
        for item in body.get("Orders") or []:
            if isinstance(item, Mapping) and item.get("ErrorInfo"):
                info = item["ErrorInfo"]
                break
    if isinstance(info, Mapping):
        code = info.get("ErrorCode")
        return code if isinstance(code, str) else "UnknownErrorInfo"
    return None


def _failed(reply: _Reply) -> bool:
    body = reply.body
    return reply.status >= 300 or _error_code(body) is not None or (
        isinstance(body, Mapping) and body.get("PreCheckResult") == "Error")


class Saxo(BaseBroker):
    broker_code = "saxo"
    supports_virtual = True
    # Class-level defaults so capabilities() can be read without a connection (broker_table probes it).
    _markets: tuple = ("US", "SG", "FX")
    _asset_types: tuple = ("Stock", "Etf", "FxSpot")

    def __init__(
        self,
        *,
        account: str,
        mode: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        token: str | None = None,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        redirect_uri: str | None = None,
        symbol_map: Mapping[str, Any] | None = None,
        markets: tuple[str, ...] = ("US", "SG", "FX"),
        asset_types: tuple[str, ...] = ("Stock", "Etf", "FxSpot"),
        base_url: str | None = None,
        token_url: str | None = None,
        on_token_refresh: Callable[[SaxoTokenState], None] | None = None,
        reload_refresh_token: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        limiter: RateLimiter | None = None,
        timeout: float = 20.0,
        order_timeout: float = 65.0,
        client: Any = None,
        client_options: Mapping | None = None,
        market: str = "US",
        venue: str | None = None,
        virtual: bool | None = None,
        **kwargs,
    ) -> None:
        options = dict(client_options or {})
        allowed = {"app_key", "app_secret", "token", "refresh_token", "expires_at", "redirect_uri", "symbol_map"}
        if options.keys() - allowed:
            raise ConfigurationError("Unknown Saxo client_options field")
        app_key = app_key if app_key is not None else options.get("app_key")
        app_secret = app_secret if app_secret is not None else options.get("app_secret")
        token = token if token is not None else options.get("token")
        refresh_token = refresh_token if refresh_token is not None else options.get("refresh_token")
        expires_at = expires_at if expires_at is not None else options.get("expires_at")
        redirect_uri = redirect_uri if redirect_uri is not None else options.get("redirect_uri")
        symbol_map = symbol_map if symbol_map is not None else options.get("symbol_map")
        # The mode is explicit and never inferred; demo and paper are both «virtual» (no real money).
        if mode not in MODES:
            raise ConfigurationError("Saxo mode must be paper, demo or live")
        if venue is not None:
            raise ConfigurationError("Saxo routes by instrument; no venue is configured")
        if virtual is not None and virtual != (mode != "live"):
            raise ConfigurationError("virtual contradicts the Saxo mode")
        # One Saxo instance serves several markets; BaseBroker only knows KR/US so it gets the US placeholder.
        super().__init__(account=account, market="US", virtual=mode != "live", **kwargs)
        self.mode = mode
        self._markets = tuple(markets)
        if not self._markets or any(m not in {"US", "SG", "FX"} for m in self._markets):
            raise ConfigurationError("Saxo markets must be a subset of US, SG, FX")
        self._asset_types = tuple(asset_types)
        self._symbols: dict[str, SaxoInstrument] = {}
        for name, entry in dict(symbol_map or {}).items():
            inst = entry if isinstance(entry, SaxoInstrument) else SaxoInstrument(*entry)
            if not isinstance(inst.uic, int) or isinstance(inst.uic, bool) or not inst.asset_type:
                raise ConfigurationError("Saxo symbol_map entries are (Uic:int, AssetType[, market])")
            self._symbols[name] = inst
        self._by_uic = {(i.uic, i.asset_type): n for n, i in self._symbols.items()}
        self._account_key = account
        self._timeout, self._order_timeout = timeout, order_timeout
        self._clock = clock
        self._on_refresh = on_token_refresh
        self._reload_refresh = reload_refresh_token
        self._http = client if client is not None else _Http()
        self._token, self._refresh_token, self._expires_at = token, refresh_token, expires_at
        self._app_key, self._app_secret, self._redirect_uri = app_key, app_secret, redirect_uri
        self._paper_orders: dict[str, Order] = {}
        if mode == "paper":
            self._base = self._token_url = ""
            self._limiter = limiter
            return
        for value in (app_key, token):
            if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
                raise ConfigurationError("Supply Saxo app_key and token")
        default_base = SAXO_SIM_URL if mode == "demo" else SAXO_LIVE_URL
        self._base = (base_url or default_base).rstrip("/")
        self._token_url = token_url or (
            self._base + "/token" if base_url else (SAXO_SIM_TOKEN_URL if mode == "demo" else SAXO_LIVE_TOKEN_URL))
        self._verify_host()
        self._limiter = limiter if limiter is not None else credential_rate_limiter(
            "saxo", app_key, {"order": 1.0, "request": 2.0})

    # ------------------------------------------------------------------ guards
    def _verify_host(self) -> None:
        # Gateway choice follows the mode and nothing else. A loopback base (contract mock) is demo-only.
        official = SAXO_SIM_URL if self.mode == "demo" else SAXO_LIVE_URL
        if self._base == official:
            return
        if self.mode == "demo" and self._base.startswith(_LOOPBACK):
            return
        raise ConfigurationError("Saxo base_url must be the official gateway for this mode")

    def _no_call_in_paper(self) -> None:
        if self.mode == "paper":
            raise NotSupportedError("Paper mode never calls Saxo")

    # ------------------------------------------------------------------ declarations
    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True, supports_market_order=True, supports_limit_order=True,
            supports_stop_order=True, supports_stop_limit_order=True,
            supports_single_order_cancel=True, supports_order_history=True, realtime_open_orders=True,
            time_in_force=tuple(_DURATION), supports_amend=True,
            markets=self._markets, asset_types=self._asset_types,
            asset_types_closed=tuple(t for t in ("CfdOnStock", "CfdOnIndex", "ContractFutures")
                                     if t not in self._asset_types),
            currency=tuple(c for c in _CURRENCY if c[0] in self._markets),
            supports_virtual_account=True, virtual=self.virtual,
        )

    def _symbol(self, symbol: str, market: str) -> str:
        if market not in self._markets:
            raise NotSupportedError("Saxo market is not declared for this account")
        return symbol

    def _instrument(self, symbol: str) -> SaxoInstrument:
        inst = self._symbols.get(symbol)
        if inst is None:
            raise NotSupportedError("Symbol has no Saxo (Uic, AssetType) mapping")
        return inst

    def _validate_order(self, request) -> None:
        super()._validate_order(request)
        inst = self._instrument(request.symbol)
        if request.market != inst.market:
            raise NotSupportedError("Request market differs from the symbol's market")
        if inst.asset_type not in self.capabilities().asset_types:
            raise NotSupportedError("Asset class is not open for orders on this build")
        if isinstance(request, OrderRequest) and request.amount is not None:
            raise NotSupportedError("Saxo adapter takes quantity orders only")

    # ------------------------------------------------------------------ token + transport
    def _expired_locally(self) -> bool:
        return self._expires_at is not None and self._clock() >= self._expires_at - _REFRESH_MARGIN_SEC

    def _refresh(self) -> None:
        """OAuth refresh (rotating). Raises BrokerRejectedError when Saxo refuses, _NoReply when unreachable."""
        if not self._refresh_token or not self._app_secret:
            raise BrokerRejectedError("Saxo token expired; reconnect the account", code="HTTP401",
                                      kind="token_expired", reason="token_expired")
        basic = "Basic " + base64.b64encode(f"{self._app_key}:{self._app_secret}".encode()).decode()
        body = None
        for attempt in range(2):
            form = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
            if self._redirect_uri:
                form["redirect_uri"] = self._redirect_uri
            reply = self._http.send("POST", self._token_url, headers={"Authorization": basic}, form=form,
                                    timeout=self._timeout)
            body = reply.body
            if reply.status == 200 and isinstance(body, Mapping) and isinstance(body.get("access_token"), str):
                break
            # Another process may have rotated the refresh token first (ours is now void): retry once with the
            # owner's stored value when it differs. Never a second retry, never with the same token.
            stored = self._stored_refresh_token() if attempt == 0 else None
            if not stored or stored == self._refresh_token:
                raise BrokerRejectedError("Saxo refused the token refresh; reconnect the account",
                                          code="invalid_grant", kind="token_expired", reason="token_expired")
            self._refresh_token = stored
        self._token = body["access_token"]
        life = body.get("expires_in")
        self._expires_at = self._clock() + float(life) if isinstance(life, (int, float)) else None
        new_refresh = body.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh:
            self._refresh_token = new_refresh  # no new one issued: the current refresh token stays valid (RFC 6749 §6)
        if self._on_refresh is not None:
            self._on_refresh(SaxoTokenState(self._token, self._expires_at, self._refresh_token))

    def _stored_refresh_token(self) -> str | None:
        if self._reload_refresh is None:
            return None
        try:
            value = self._reload_refresh()
        except Exception:  # a failed owner read leaves the refusal as it is
            return None
        return value if isinstance(value, str) and value else None

    def _try_refresh(self) -> bool:
        try:
            self._refresh()
            return True
        except (BrokerRejectedError, _NoReply):
            return False

    def _send(self, method: str, path: str, *, body: Any = None, bucket: str = "request",
              timeout: float | None = None) -> _Reply:
        self._no_call_in_paper()
        self._verify_host()
        if self._limiter is not None:
            self._limiter.wait(bucket)
        return self._http.send(method, self._base + path, headers={"Authorization": f"Bearer {self._token}"},
                               body=body, timeout=timeout or self._timeout)

    def _read(self, path: str) -> Any:
        """Idempotent GET: one refresh-and-retry on 401, then any failure is an invalid (unknown) read."""
        self._no_call_in_paper()
        if self._expired_locally():
            self._try_refresh()
        for attempt in (0, 1):
            try:
                reply = self._send("GET", path)
            except _NoReply:
                raise TransportError("Saxo read failed; nothing was written") from None
            if reply.status == 401 and attempt == 0 and self._try_refresh():
                continue
            if _failed(reply) or reply.body is None:
                raise InvalidResponseError("Saxo read refused or invalid")
            return reply.body
        raise InvalidResponseError("Saxo read refused")  # pragma: no cover

    def _write(self, method: str, path: str, body: Any = None) -> _Reply:
        """One order write. Never re-sent. Returns a successful reply or raises the SEAM §4 class."""
        self._no_call_in_paper()
        if self._expired_locally():
            # Nothing has been sent yet: a failed refresh is a clean «not accepted».
            try:
                self._refresh()
            except (BrokerRejectedError, _NoReply):
                raise BrokerRejectedError("Saxo token could not be refreshed; order not sent", code=None,
                                          kind=REJECTED, reason="token_refresh_failed") from None
        try:
            reply = self._send(method, path, body=body, bucket="order", timeout=self._order_timeout)
        except _NoReply:
            raise TransportError("Saxo order write got no answer; reconcile before retrying") from None
        if not _failed(reply):
            return reply
        table = DEFINITIVE_REJECTS["saxo"]
        body = reply.body if isinstance(reply.body, Mapping) else {}  # a non-JSON body is «no body»: status only
        order_id = body.get("OrderId") if isinstance(body.get("OrderId"), str) and body.get("OrderId") else None
        if reply.status == 401:
            self._try_refresh()  # next call carries a fresh token; this order is NOT re-sent
            code = "HTTP401"
        elif reply.status == 429:
            code = "HTTP429"
        elif reply.status >= 500 or reply.status in (409, 202) or order_id is not None:
            # 5xx (even with ErrorInfo), 409 duplicate, 202 TradeNotCompleted, or an OrderId next to ErrorInfo
            # (the main order was accepted, only a related order failed): never a rejection.
            code = None
        else:
            code = _error_code(reply.body)
        entry = table.get(code) if code else None
        if entry is not None:
            raise BrokerRejectedError("Broker did not accept the request", code=code, kind=entry.kind,
                                      reason=entry.reason)
        raise SaxoOutcomeUnknownError(
            "Saxo order outcome unknown; reconcile by ExternalReference before retrying", order_id)

    # ------------------------------------------------------------------ order bodies
    @staticmethod
    def _duration(request: OrderRequest) -> dict:
        duration = {"DurationType": _DURATION[request.time_in_force or "day"]}
        if request.time_in_force == "gtd":
            duration["ExpirationDateTime"] = request.expire_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return duration

    @staticmethod
    def _prices(request: OrderRequest) -> dict:
        if request.order_type == "stop":
            return {"OrderPrice": float(request.stop_price)}
        out = {}
        if request.price is not None:
            out["OrderPrice"] = float(request.price)
        if request.order_type == "stop_limit":
            out["StopLimitPrice"] = float(request.stop_price)
        return out

    def _place_body(self, request: OrderRequest) -> dict:
        inst = self._instrument(request.symbol)
        return {
            "AccountKey": self._account_key, "Uic": inst.uic, "AssetType": inst.asset_type,
            "BuySell": "Buy" if request.side == "buy" else "Sell", "Amount": int(request.quantity),
            "OrderType": _ORDER_TYPE[request.order_type], "OrderDuration": self._duration(request),
            "ManualOrder": False, "ExternalReference": external_reference(request.client_key),
            **self._prices(request),
        }

    # ------------------------------------------------------------------ writes
    def _paper(self, request, status: str) -> Order:
        number = f"paper-{len(self._paper_orders) + 1}"
        order = Order(id=getattr(request, "order_id", None) or number, symbol=request.symbol, market=request.market,
                      status=status, virtual=True)
        self._paper_orders[order.id] = order
        return order

    def _place_order(self, request: OrderRequest) -> Order:
        if self.mode == "paper":
            return self._paper(request, "accepted")
        body = self._place_body(request)
        reply = self._write("POST", "/trade/v2/orders", body)
        data = reply.body
        order_id = data.get("OrderId") if isinstance(data, Mapping) else None
        if not isinstance(order_id, str) or not order_id:
            raise InvalidResponseError("Saxo accepted without an OrderId; reconcile before retrying")
        echoed = data.get("ExternalReference")
        if echoed is not None and echoed != body["ExternalReference"]:
            raise SaxoOutcomeUnknownError("Saxo ExternalReference echo differs; reconcile before retrying", order_id)
        return Order(id=order_id, symbol=request.symbol, market=request.market, side=request.side,
                     order_type=request.order_type, quantity=request.quantity, price=request.price,
                     status="accepted", virtual=self.virtual, external_reference=body["ExternalReference"])

    def amend_order(self, request: AmendRequest) -> Order:
        """Optional extension (AmendableBroker). Not on BaseBroker: kis/dbfi/toss/kiwoom stay without it."""
        request = AmendRequest.model_validate(request.model_dump())
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._amend_order(request)))

    def _amend_order(self, request: AmendRequest) -> Order:
        if self.mode == "paper":
            return self._paper(request, "accepted")
        row = self._find_row(request.order_id)
        if row is None:
            raise BrokerRejectedError("Order is not a working Saxo order", code=None, kind=REJECTED,
                                      reason="order_not_found")
        if row.get("Status") == "Filled":
            raise BrokerRejectedError("Order is already filled", code="TooLateToChangeOrder", kind=REJECTED,
                                      reason="business_rejected")
        filled = Decimal(str(row.get("FilledAmount") or 0))
        total = filled + Decimal(int(request.quantity))
        body = {"AccountKey": self._account_key, "OrderId": request.order_id, "Amount": int(total),
                "AssetType": row.get("AssetType") or self._instrument(request.symbol).asset_type,
                "OrderType": _ORDER_TYPE[request.order_type], **self._prices(request)}
        if request.time_in_force is not None:
            body["OrderDuration"] = self._duration(request)
        elif row.get("Duration"):
            body["OrderDuration"] = row["Duration"]
        reply = self._write("PATCH", "/trade/v2/orders", body)
        order_id = reply.body.get("OrderId") if isinstance(reply.body, Mapping) else None
        if not isinstance(order_id, str) or not order_id:
            raise InvalidResponseError("Saxo accepted the amendment without an OrderId; reconcile")
        return Order(id=order_id, symbol=request.symbol, market=request.market, side=request.side,
                     order_type=request.order_type, quantity=int(total), price=request.price, filled=int(filled),
                     remaining=int(request.quantity), status="accepted", virtual=self.virtual)

    def _cancel_order(self, request: CancelRequest) -> Order:
        if self.mode == "paper":
            return self._paper(request, "cancelled").model_copy(update={"id": request.order_id})
        query = urllib.parse.urlencode({"AccountKey": self._account_key})
        self._write("DELETE", f"/trade/v2/orders/{urllib.parse.quote(request.order_id, safe='')}?{query}")
        # Acknowledged is not cancelled: a fill can race the cancel, and an order missing from the order list may be
        # filled or cancelled alike. Only a still-listed row or the order's last activity decides; else unknown.
        unknown = Order(id=request.order_id, symbol=request.symbol, market=request.market, status="unknown",
                        virtual=self.virtual)
        try:
            row = self._find_row(request.order_id, closed_too=True)
            if row is not None:
                seen = self._order_from_row(row)
                if seen.status in {"filled", "cancelled", "rejected"}:
                    return seen.model_copy(update={"symbol": request.symbol, "market": request.market})
                # Cancel still pending at Saxo: keep what has filled so far, never close it.
                return unknown.model_copy(update={"filled": seen.filled, "remaining": seen.remaining,
                                                  "quantity": seen.quantity})
            done = self._closed_by_activity(request.order_id)
        except (TransportError, InvalidResponseError):
            return unknown
        if done is None:
            return unknown
        return done.model_copy(update={"symbol": request.symbol, "market": request.market})

    def _closed_by_activity(self, order_id: str) -> Order | None:
        """The order's last activity, when it is final (filled or cancelled with its FilledAmount); else None."""
        query = urllib.parse.urlencode({"AccountKey": self._account_key, "OrderId": order_id, "EntryType": "Last"})
        rows = [r for r in self._rows(f"/cs/v1/audit/orderactivities?{query}") if str(r.get("OrderId")) == order_id]
        if len(rows) != 1:
            return None  # none (not logged yet) or several «last» rows: neither closes the order
        row = rows[0]
        status = _ACTIVITY_FINAL.get(row.get("Status"))
        if status is None or row.get("SubStatus") != "Confirmed":
            return None
        amount = decimal(row.get("Amount"))  # malformed numbers raise InvalidResponseError: the caller keeps unknown
        raw_filled = row.get("FilledAmount")
        if raw_filled in (None, ""):
            if status != "filled":
                return None  # a cancel without its filled amount could hide a partial fill
            raw_filled = row["Amount"]
        filled = decimal(raw_filled)
        if filled > amount or (status == "filled" and filled != amount):
            return None
        whole = amount == amount.to_integral_value() and filled == filled.to_integral_value()
        price = row.get("AveragePrice") if row.get("AveragePrice") not in (None, "", 0) else row.get("ExecutionPrice")
        uic, asset = row.get("Uic"), row.get("AssetType")
        return Order(
            id=order_id, symbol=self._name(uic, asset), market=self._market_of(uic, asset),
            side={"Buy": "buy", "Sell": "sell"}.get(row.get("BuySell")),
            quantity=int(amount) if whole else None, quantity_exact=amount,
            filled=int(filled) if whole else None, remaining=int(amount - filled) if whole else None,
            fill_price=Decimal(str(price)) if price not in (None, "", 0) and filled > 0 else None,
            status=status, virtual=self.virtual)

    # ------------------------------------------------------------------ reads
    def _rows(self, path: str) -> list[Mapping]:
        rows: list[Mapping] = []
        sep = "&" if "?" in path else "?"
        next_path = f"{path}{sep}$top={_PAGE}"
        for _ in range(_MAX_PAGES):
            data = self._read(next_path)
            page = data.get("Data") if isinstance(data, Mapping) else None
            if not isinstance(page, list) or any(not isinstance(r, Mapping) for r in page):
                raise InvalidResponseError("Saxo list response is malformed")
            rows.extend(page)
            nxt = data.get("__next")
            if not nxt:
                return rows
            if not isinstance(nxt, str) or not nxt.startswith("/"):
                raise InvalidResponseError("Saxo continuation is not a relative path")
            next_path = nxt
        raise InvalidResponseError("Saxo list pagination exceeded its bounded budget")

    def _find_row(self, order_id: str, *, closed_too: bool = False) -> Mapping | None:
        path = "/port/v1/orders/me" + ("?Status=All" if closed_too else "")
        matches = [r for r in self._rows(path) if str(r.get("OrderId")) == order_id]
        if len(matches) > 1:
            raise InvalidResponseError("Saxo lists one order id twice")
        return matches[0] if matches else None

    def _name(self, uic: Any, asset: Any) -> str:
        return self._by_uic.get((uic, asset)) or f"saxo:{uic}:{asset}"

    def _market_of(self, uic: Any, asset: Any) -> str:
        inst = self._symbols.get(self._by_uic.get((uic, asset), ""))
        return inst.market if inst else ("FX" if asset == "FxSpot" else "UNKNOWN")

    def _order_from_row(self, row: Mapping) -> Order:
        try:
            order_id = str(row["OrderId"])
            uic, asset = row["Uic"], row["AssetType"]
            amount = decimal(row["Amount"])
        except KeyError:
            raise InvalidResponseError("Saxo order row misses a required field") from None
        filled = decimal(row.get("FilledAmount") or 0)
        if filled > amount:
            raise InvalidResponseError("Saxo order row fills more than it holds")
        remaining = amount - filled
        raw = row.get("Status")
        if raw == "Filled" and remaining == 0:
            status = "filled"
        elif raw in ("Cancelled", "Expired"):  # not in the documented enum: defensive, never read as open
            status = "cancelled"
        elif raw == "Rejected":
            status = "rejected"
        elif raw in _OPEN:
            status = "partial" if filled > 0 and remaining > 0 else "open"
        else:
            status = "unknown"  # Parked, Unknown, a status the docs do not list: never closes the order
        whole = amount == amount.to_integral_value() and filled == filled.to_integral_value()
        price = row.get("Price")
        return Order(
            id=order_id, symbol=self._name(uic, asset), market=self._market_of(uic, asset),
            side={"Buy": "buy", "Sell": "sell"}.get(row.get("BuySell")),
            order_type=_ORDER_TYPE_BACK.get(row.get("OpenOrderType")),
            quantity=int(amount) if whole else None, quantity_exact=amount,
            price=Decimal(str(price)) if price not in (None, "", 0) else None,
            filled=int(filled) if whole else None, remaining=int(remaining) if whole else None,
            fill_price=Decimal(str(row["AveragePrice"])) if row.get("AveragePrice") not in (None, "", 0) and filled > 0
            else None,
            status=status, virtual=self.virtual, external_reference=row.get("ExternalReference"),
        )

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        wanted = None
        if symbol is not None:
            inst = self._instrument(symbol)
            wanted = (inst.uic, inst.asset_type)

        def read():
            # Unfilled orders only: Saxo's default filter is Working; a row that is filled, fully filled or
            # cancelled is dropped even if the gateway sends it, so history never reads as «open».
            rows = self._rows("/port/v1/orders/me")
            out = [self._order_from_row(r) for r in rows if wanted is None or (r.get("Uic"), r.get("AssetType")) == wanted]
            return [o for o in out if o.status not in {"filled", "cancelled", "rejected"} and not (
                o.remaining is not None and o.remaining == 0)]
        return self._call(read)

    def status(self, order: Order) -> Order:
        def read():
            row = self._find_row(order.id)
            if row is None:
                try:
                    row = self._find_row(order.id, closed_too=True)
                except (InvalidResponseError, TransportError):
                    row = None
            if row is not None:
                return self._order_from_row(row)
            # Missing from the order list proves neither a fill nor a cancellation: ask the order's last activity.
            try:
                done = self._closed_by_activity(order.id)
            except (InvalidResponseError, TransportError):
                done = None
            if done is None:
                return order.model_copy(update={"status": "unknown"})
            return done.model_copy(update={"symbol": order.symbol or done.symbol, "market": order.market or done.market,
                                           "order_type": order.order_type or done.order_type,
                                           "price": order.price, "external_reference": order.external_reference})
        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read():
            grouped: dict[tuple, list[tuple[Decimal, Decimal, Decimal]]] = {}
            for row in self._rows("/port/v1/positions/me"):
                base, view = row.get("PositionBase"), row.get("PositionView") or {}
                if not isinstance(base, Mapping):
                    raise InvalidResponseError("Saxo position row misses PositionBase")
                signed = Decimal(str(base.get("Amount")))
                if signed < 0:
                    raise InvalidResponseError("Saxo short position: pykbroker Position has no short side")
                if signed == 0:
                    continue
                key = (base.get("Uic"), base.get("AssetType"))
                last = Decimal(str(view.get("CurrentPrice", base.get("OpenPrice", 0))))
                grouped.setdefault(key, []).append((signed, Decimal(str(base.get("OpenPrice", 0))), last))
            result = []
            for (uic, asset), legs in grouped.items():
                total = sum((q for q, _, _ in legs), Decimal(0))
                avg = sum((q * p for q, p, _ in legs), Decimal(0)) / total
                last = legs[-1][2]
                whole = total == total.to_integral_value()
                result.append(Position(
                    symbol=self._name(uic, asset), market=self._market_of(uic, asset),
                    quantity=int(total) if whole else int(total.to_integral_value(rounding="ROUND_FLOOR")),
                    sellable=int(total) if whole else int(total.to_integral_value(rounding="ROUND_FLOOR")),
                    average_price=avg, last=last, market_value=last * total,
                    quantity_exact=None if whole else total, sellable_exact=None if whole else total))
            return result
        return self._call(read)

    def get_balance(self) -> Balance:
        def read():
            data = self._read("/port/v1/balances/me")
            if not isinstance(data, Mapping) or not isinstance(data.get("Currency"), str):
                raise InvalidResponseError("Saxo balance response is malformed")
            cash = decimal(data["CashAvailableForTrading"])
            total, cash_balance = data.get("TotalValue"), data.get("CashBalance")
            value = Decimal(str(total)) - Decimal(str(cash_balance)) if total is not None and cash_balance is not None \
                else Decimal(0)
            return Balance(currency=data["Currency"], cash_available=cash, position_value=max(value, Decimal(0)))
        return self._call(read)

    def get_quote(self, symbol: str, *, market: str = "US") -> Quote:
        raise NotSupportedError("Saxo quotes (infoprices) are not implemented")

    def get_rules(self, symbol: str, *, now=None, trading_day=None) -> Rules:
        inst = self._instrument(symbol)
        currency = dict(_CURRENCY).get(inst.market, "USD")
        return Rules(symbol=symbol, market=inst.market, currency=currency, timezone=_TIMEZONE[inst.market],
                     halted=self._halt_from_provider(symbol))  # session unknown => not tradable (conservative)
