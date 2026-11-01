"""Meritz Securities KR/US REST broker: reads, limit orders, modify and single cancel, locked by default.

Contract source: the official Meritz Open API spec (openapi.json 2026-10-28, llms.txt, docs/errors.md from
github.com/meritz-securities/open-api, read 2026-11-01). There is no Meritz mock server or mock account: the account
bound to an app key is the real account, so ``virtual=True`` is refused.

Rules this adapter keeps (design 2026-11-01):

* Writes are locked twice: the module switch ``MERITZ_SPEC_VERIFIED`` (False until a human walks the real server with a
  small order) and ``allow_live_orders`` on the instance. Reads are never locked.
* A response is judged body first (the gateway answers every error as HTTP 500), then, for a write, ``warn_cls_code``
  (anything but "0" means NOT accepted), then ``rsp_cd``. ``5820`` never decides emptiness: ``data`` does.
* A write is sent exactly once. No retry, no automatic ``warn_cnfr_yn=Y`` resend: the warning text goes back to the
  caller. A lost or unreadable answer is unknown; the order list is the only thing that settles it.
* Vendor text never enters an exception message; only the sanitized code does (plus the warning text, which the caller
  shows to the customer, on ``BrokerRejectedError.warn_message``).
"""

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.models.order import OrderStatus, Side
from pykbroker.rules.kr import KST, equity_rules, normalize_symbol
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, PageRows, decimal, optional_price, whole

MERITZ_SPEC_VERIFIED = False  # Global veto only; True never enables live orders by itself.
MERITZ_API_BASE_URL = "https://openapi.imeritz.com:9443"  # the only server in the spec (no mock exists)

CLIENT_OPTION_KEYS = {"app_key", "app_secret", "token", "token_type", "expires_at", "mac_address"}
TOKEN_MARGIN = timedelta(seconds=60)  # the official example refreshes 60 s before expiry
MAX_PAGES = 20
# official corrections.json: the KR quote APIs answer HTTP 200 with good data and a corrupted rsp_cd
DATA_SUCCESS_PATHS = frozenset({"/market/v1/prices", "/market/v1/orderbook"})
READ_RATE_RETRIES = 2

SUCCESS_CODES = frozenset({"0000", "0001", "5620", "5762", "5766", "5820", "5822"})
# Codes that prove the gateway/business layer refused before or without taking the order.
GATEWAY_REFUSALS = frozenset({"EGW00103", "EGW00202", "0760", "IGW50024", "IGW50004", "IGW50006", "IGW50002"})
TOKEN_REFUSALS = frozenset({"EGW00121", "EGW00122", "EGW00123"})
RATE_LIMIT_CODE = "EGW00200"
SERVER_ERROR_CODE = "EGW00002"  # «서버측 오류»: not a refusal, the order may or may not exist
_BUSINESS_CODE = re.compile(r"[0-9]{4}")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_-]{1,40}")

KR_READ_PATHS = (
    "/market/v1/prices",
    "/market/v1/orderbook",
    "/accounts/v1/me/deposit",
    "/accounts/v1/me/holdings",
    "/trading/v1/orders/history",
)
US_READ_PATHS = (
    "/market/v1/overseas/prices",
    "/market/v1/overseas/orderbook",
    "/trading/v1/overseas/orders/estimate",
    "/accounts/v1/me/overseas/holdings",
    "/trading/v1/overseas/orders/history",
)
WRITE_PATHS = (
    "/trading/v1/orders/buy",
    "/trading/v1/orders/sell",
    "/trading/v1/orders/modify",
    "/trading/v1/orders/cancel",
    "/trading/v1/overseas/orders/buy",
    "/trading/v1/overseas/orders/sell",
    "/trading/v1/overseas/orders/modify",
    "/trading/v1/overseas/orders/cancel",
)
# Spec: «초당 호출 한도는 API마다 10건». Paths are the API identity.
MERITZ_RATES: dict[str, float] = {
    path: 10.0 for path in (*KR_READ_PATHS, *US_READ_PATHS, *WRITE_PATHS, "/oauth2/token")
}

# venue -> (quote exchange code mrkt_cls_code, order exchange code ovrs_exch_vndr_idno)
_US_VENUES: dict[str, tuple[str, str]] = {"NYSE": ("NY", "0321"), "NASDAQ": ("OQ", "0537"), "AMEX": ("AX", "0066")}
KR_LIMIT = "01"
US_LIMIT = "01"
KR_KRX = "01"
_MAC = re.compile(r"[0-9A-F]{12}")
_ORDER_NO = re.compile(r"[0-9]{1,12}")

_TOKEN_LOCK = threading.Lock()
_TOKENS: dict[str, tuple[str, datetime]] = {}  # app-key digest -> (access token, usable until)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_code(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if _SAFE_CODE.fullmatch(text) else None


def _digest(app_key: str) -> str:
    return hashlib.sha256(app_key.encode()).hexdigest()


def default_mac_address() -> str:
    """The official example's device value (uuid.getnode), 12 upper-case hex digits."""
    return f"{uuid.getnode():012X}"[-12:]


class _MeritzRestClient:
    """One HTTP attempt through the shared no-redirect boundary. The HTTP status is returned, never judged here:
    the gateway sends every error as 500 with a readable body."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self.base_url = (base_url or MERITZ_API_BASE_URL).rstrip("/")
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        form: bool = False,
    ) -> tuple[int, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if body is None:
            data = None
        elif form:
            data = urllib.parse.urlencode(body).encode("utf-8")
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            exc.close()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except ValueError:
                raise InvalidResponseError("Meritz error response is not JSON") from None


def _order_number(value: Any) -> str:
    """oder_no is a positive integer (JSON number). 0 is the «no original order» filler, never an order."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidResponseError("Meritz order number is missing; reconcile before retrying")
    text = str(value).strip()
    if not (_ORDER_NO.fullmatch(text) and int(text)):
        raise InvalidResponseError("Meritz order number is not the documented form; reconcile before retrying")
    return str(int(text))


def _rows(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = body.get("data")
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
        raise InvalidResponseError("Meritz list field must be a list of rows")
    return list(data)


def _symbol_code(value: Any) -> str:
    return str(value).strip().removeprefix("A")


class Meritz(BaseBroker):
    broker_code = "meritz"
    supports_virtual = False  # the official docs: «모의계좌는 없습니다»
    us_venues = tuple(_US_VENUES)

    def __init__(
        self,
        *,
        account: str,
        client_options: Mapping[str, Any],
        client_factory: Callable[..., Any] | None = None,
        rate_limiter: RateLimiter | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        virtual: bool = False,
        venue: str | None = None,
        is_integrated: bool = False,
        allow_live_orders: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if virtual is not False:
            if not isinstance(virtual, bool):
                raise ConfigurationError("virtual must be a boolean; the account mode is never inferred")
            raise ConfigurationError("메리츠증권은 모의 계좌가 없습니다")
        super().__init__(account=account, safety=safety, market=market, virtual=False, halt_provider=halt_provider)
        if type(allow_live_orders) is not bool or not isinstance(is_integrated, bool):
            raise ConfigurationError("allow_live_orders and is_integrated must be booleans")
        if market == "US" and venue not in _US_VENUES:
            raise ConfigurationError("Meritz US requires NYSE, NASDAQ or AMEX venue")
        if market == "KR" and venue not in (None, "KRX"):
            raise NotSupportedError("Meritz KR supports KRX only")
        options = dict(client_options)
        if not set(options) <= CLIENT_OPTION_KEYS:
            raise ConfigurationError("Unknown Meritz client option")
        app_key, app_secret = options.get("app_key"), options.get("app_secret")
        if not isinstance(app_key, str) or not app_key.strip() or len(app_key) > 36:
            raise ConfigurationError("Supply a Meritz app_key (36 characters or fewer)")
        if app_secret is not None and (
            not isinstance(app_secret, str) or not app_secret.strip() or len(app_secret) > 36
        ):
            raise ConfigurationError("Meritz app_secret must be a non-empty string of 36 characters or fewer")
        token = options.get("token")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ConfigurationError("Meritz stored token must be non-empty")
        if str(options.get("token_type", "Bearer")).lower() != "bearer":
            raise ConfigurationError("Only Bearer tokens are verified")
        if token is None and app_secret is None:
            raise ConfigurationError("Supply a stored token or the app_secret to issue one")
        expires_at = options.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, datetime):
            raise ConfigurationError("expires_at must be a datetime")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=KST)  # the stored value is a KST wall clock
        mac = options.get("mac_address") or default_mac_address()
        if not isinstance(mac, str) or not _MAC.fullmatch(mac):
            raise ConfigurationError("mac_address must be 12 upper-case hex digits")
        self.venue = venue
        self.is_integrated = is_integrated
        self._app_key, self._app_secret = app_key, app_secret
        self._token, self._expires_at, self._mac = token, expires_at, mac
        self._allow_live_orders = allow_live_orders
        self._clock, self._sleep = clock, sleeper
        self._limiter = rate_limiter or credential_rate_limiter("meritz", app_key, MERITZ_RATES)
        self._client = (client_factory or _MeritzRestClient)(base_url=MERITZ_API_BASE_URL)

    # ------------------------------------------------------------------ locks and declarations
    def _writes_verified(self) -> bool:
        return MERITZ_SPEC_VERIFIED is True and getattr(self, "_allow_live_orders", False) is True

    def _require_writes(self) -> None:
        if not self._writes_verified():
            raise SpecUnverifiedError("Meritz writes are locked: spec not verified or live orders not allowed")

    def capabilities(self) -> Capabilities:
        # Declaration only (read from an instance-less probe): single cancel exists in the spec; whether a write may
        # actually leave is the lock's answer, so limit orders follow the lock. Market orders stay off until a real
        # small order shows the exchange values (research unknown 11).
        return Capabilities(
            supports_spot=True,
            supports_limit_order=self._writes_verified(),
            supports_market_order=False,
            supports_single_order_cancel=True,
            supports_nxt=False,
            supports_order_history=True,
            realtime_open_orders=False,
            supports_virtual_account=False,
            virtual=False,
            requires_venue=self.market == "US",
            venues=self.us_venues if self.market == "US" else ("KRX",),
            markets=(self.market,),
        )

    # ------------------------------------------------------------------ token
    def _usable(self, expires_at: datetime | None) -> bool:
        return expires_at is None or self._clock() < expires_at - TOKEN_MARGIN

    def _drop_token(self) -> None:
        digest = _digest(self._app_key)
        with _TOKEN_LOCK:
            _TOKENS.pop(digest, None)
        self._token, self._expires_at = None, None

    def _issue_token(self) -> str:
        if self._app_secret is None:
            raise ConfigurationError("The stored Meritz token is expired and no app_secret is held to issue another")
        body = {
            "grant_type": "client_credentials",
            "client_id": self._app_key,
            "client_secret": self._app_secret,
            "scope": "login",
        }
        self._limiter.wait("/oauth2/token")
        status, answer = self._client.request(
            "POST",
            "/oauth2/token",
            headers={"content-type": "application/x-www-form-urlencoded"},  # the spec's only token content type
            body=body,
            form=True,
        )
        if not isinstance(answer, Mapping):
            raise InvalidResponseError("Meritz token response is not an object")
        if "access_token" not in answer:
            code = _safe_code(answer.get("rsp_cd"))
            if code is not None and code != SERVER_ERROR_CODE:
                raise BrokerRejectedError(
                    "Meritz refused the credentials", code=code, kind="rejected", reason="credentials_rejected"
                )
            raise InvalidResponseError("Meritz token response has no access_token")
        token, lifetime = answer["access_token"], answer.get("expires_in")
        if not (isinstance(token, str) and token) or str(answer.get("token_type", "Bearer")).lower() != "bearer":
            raise InvalidResponseError("Meritz token response is invalid")
        if (
            isinstance(lifetime, bool)
            or not str(lifetime).isascii()
            or not str(lifetime).isdigit()
            or int(str(lifetime)) <= 0
        ):
            raise InvalidResponseError("Meritz token lifetime is invalid")
        expires_at = self._clock() + timedelta(seconds=int(str(lifetime)))
        with _TOKEN_LOCK:
            _TOKENS[_digest(self._app_key)] = (token, expires_at)
        self._token, self._expires_at = token, expires_at
        return token

    def _bearer(self) -> str:
        with _TOKEN_LOCK:
            cached = _TOKENS.get(_digest(self._app_key))
        if cached is not None and self._usable(cached[1]):
            return cached[0]
        if self._token is not None and self._usable(self._expires_at):
            return self._token
        return self._issue_token()

    # ------------------------------------------------------------------ transport and judgement
    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        write: bool = False,
    ) -> Mapping[str, Any]:
        token = self._bearer()  # a separate token call, never part of the order write
        headers = {"authorization": f"Bearer {token}", "mac_address": self._mac}
        if body is not None:
            headers["content-type"] = "application/json"
        self._limiter.wait(path)
        status, answer = self._client.request(method, path, headers=headers, params=params, body=body)
        return self._judge(answer, write=write, path=path)

    def _judge(self, answer: Any, *, write: bool, path: str) -> Mapping[str, Any]:
        if not isinstance(answer, Mapping) or not isinstance(answer.get("rsp_cd"), str):
            raise InvalidResponseError("Meritz response has no readable rsp_cd; reconcile before retrying")
        code = answer["rsp_cd"]
        data = answer.get("data")
        if not write and path in DATA_SUCCESS_PATHS and isinstance(data, Mapping) and data:
            # corrections.json: these quote APIs send a healthy `data` with a damaged rsp_cd (NUL bytes). An error
            # answer carries no `data`, so a non-empty object is the evidence of success here.
            return answer
        if write and isinstance(data, Mapping) and "warn_cls_code" in data and data["warn_cls_code"] != "0":
            # Checked before rsp_cd: a «0001» with a warning is NOT an accepted order. Never auto-resent.
            error = BrokerRejectedError(
                "Meritz did not accept the order (warning)",
                code=f"warn_{_safe_code(data['warn_cls_code']) or 'x'}",
                kind="rejected",
                reason="meritz_warning",
            )
            error.warn_message = str(data.get("warn_msg") or "")[:100]
            raise error
        if code in SUCCESS_CODES:
            return answer
        safe = _safe_code(code)
        if code in TOKEN_REFUSALS:
            self._drop_token()
            if write:
                raise BrokerRejectedError(
                    "Meritz refused the token", code=safe, kind="token_expired", reason="token_expired"
                )
            raise _TokenExpired
        if code == RATE_LIMIT_CODE:
            if write:
                raise BrokerRejectedError(
                    "Meritz throttled the call", code=safe, kind="rate_limited", reason="rate_limited"
                )
            raise _RateLimited
        if write:
            if code in GATEWAY_REFUSALS or _BUSINESS_CODE.fullmatch(code):
                raise BrokerRejectedError(
                    "Meritz rejected the order", code=safe, kind="rejected", reason="broker_rejected"
                )
            # EGW00002 and any code this adapter does not know: the order may exist.
            raise InvalidResponseError("Meritz write failed with an unclassified code; reconcile before retrying")
        raise BrokerRejectedError("Meritz rejected the request", code=safe, kind="rejected", reason="broker_rejected")

    def _read(self, path: str, *, params: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        """A GET: an expired token is re-issued once, a throttle waited out briefly; neither can duplicate an order."""
        reissued = False
        throttled = 0
        while True:
            try:
                return self._send("GET", path, params=params)
            except _TokenExpired:
                if reissued:
                    raise BrokerRejectedError(
                        "Meritz keeps refusing the token", code="EGW00123", kind="token_expired", reason="token_expired"
                    ) from None
                reissued = True
            except _RateLimited:
                throttled += 1
                if throttled > READ_RATE_RETRIES:
                    raise BrokerRejectedError(
                        "Meritz throttled the read", code=RATE_LIMIT_CODE, kind="rate_limited", reason="rate_limited"
                    ) from None
                self._sleep(0.2 * throttled)

    def _paged(
        self, path: str, params: Mapping[str, str], row_key: tuple[str, ...] | None = None
    ) -> list[Mapping[str, Any]]:
        """Every page; a list whose end cannot be shown is refused, never cut short."""
        paging = PageRows(row_key, "Meritz") if row_key else None
        rows: list[Mapping[str, Any]] = []
        query = dict(params)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            body = self._read(path, params=query)
            page = _rows(body)
            if paging is not None:
                paging.add(page)
            else:
                rows.extend(page)
            more = body.get("tr_cont")
            if more in (None, "", "0", 0):
                return paging.rows if paging is not None else rows
            key = body.get("tr_cont_key")
            if more not in ("1", 1) or not isinstance(key, str) or not key or key in seen:
                raise InvalidResponseError("Meritz continuation does not show where the list ends")
            seen.add(key)
            query = {**params, "tr_cont": "1", "tr_cont_key": key}  # the key goes back byte for byte
        raise InvalidResponseError("Meritz list exceeded the page limit; refusing a truncated result")

    # ------------------------------------------------------------------ symbols and venues
    def _kr_code(self, symbol: str) -> str:
        return normalize_symbol(symbol)

    def _us_codes(self) -> tuple[str, str]:
        if self.venue not in _US_VENUES:
            raise ConfigurationError("Meritz US requires NYSE, NASDAQ or AMEX venue")
        return _US_VENUES[self.venue]

    # ------------------------------------------------------------------ reads
    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        code = self._symbol(symbol, market)

        def read() -> Quote:
            if self.market == "US":
                quote_code, _ = self._us_codes()
                base = {"mrkt_div_code": "OV", "mrkt_cls_code": quote_code, "iscd": code, "dely_rltm_cls_code": "0"}
                last = decimal(self._read("/market/v1/overseas/prices", params=base)["data"]["prpr"])
                book = self._read("/market/v1/overseas/orderbook", params=base)["data"]
                bid, ask = optional_price(book.get("bidp1")), optional_price(book.get("askp1"))
            else:
                base = {"mrkt_div_code": "J", "iscd": code}
                last = decimal(self._read("/market/v1/prices", params=base)["data"]["stck_prpr"])
                book = self._read("/market/v1/orderbook", params=base)["data"]
                bid, ask = optional_price(book.get("bidp")), optional_price(book.get("askp"))
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None  # a crossed pair is not a spread anyone can trade on
            return Quote(symbol=code, market=self.market, last=last, bid=bid, ask=ask)

        return self._call(read)

    def get_rules(
        self, symbol: str, *, now: datetime | None = None, trading_day: Callable[[date], bool] | None = None
    ) -> Rules:
        if self.market == "US":
            return super().get_rules(self._symbol(symbol, "US"), now=now, trading_day=trading_day)
        code = self._symbol(symbol, "KR")

        def read() -> tuple[Decimal, Decimal | None, Decimal | None, bool | None]:
            row = self._read("/market/v1/prices", params={"mrkt_div_code": "J", "iscd": code})["data"]
            last = decimal(row["stck_prpr"])
            upper, lower = optional_price(row.get("mxpr")), optional_price(row.get("llam"))
            if upper is None or lower is None or not lower <= last <= upper:
                upper = lower = None  # half a band, or one that excludes the last price, is not a band
            halted = {"Y": True}.get(str(row.get("tmpr_susp_yn")))  # a native «N» does not prove tradable
            return last, upper, lower, halted

        last, upper, lower, native_halt = self._call(read)
        provider = self._halt_from_provider(code)
        halted = True if native_halt or provider is True else provider
        return equity_rules(
            code, last, halted=halted, now=now, trading_day=trading_day, limit_up=upper, limit_down=lower
        )

    def _cash(self) -> Decimal:
        if self.market == "US":
            rows = self._read("/trading/v1/overseas/orders/estimate", params={"intg_marg_iqry_yn": "N"})
            usd = [row for row in _rows(rows) if row.get("crcd") == "USD"]
            if len(usd) != 1:
                raise InvalidResponseError("Meritz cash must contain exactly one USD row")
            return min(decimal(usd[0]["oder_crnc_oder_able_camt"]), decimal(usd[0]["stnd_deps_camt"]))
        data = self._read("/accounts/v1/me/deposit")["data"]
        # oder_able_cash_amt is cash only (substitute collateral is a different field); capped by the cash balance.
        return min(decimal(data["oder_able_cash_amt"]), decimal(data["tfam"]))

    def available_cash(self) -> Decimal:
        return self._call(self._cash)

    def _positions(self) -> list[Position]:
        if self.market == "US":
            return self._us_positions()
        rows = self._paged(
            "/accounts/v1/me/holdings",
            {"nthn_rmnd_incu_yn": "N"},
            ("iscd", "mksc_cls_name", "loan_date", "loan_mtrt_date", "loan_dtl_type_code"),
        )
        committed = self._open_sell_quantity()
        positions = []
        for row in rows:
            # Only ordinary domestic holdings: credit rows and overseas rows are other accounts' business here.
            if row.get("pros_trgt_cls_code") != "1" or row.get("mksc_cls_name") != "주식일반":
                continue
            code = _symbol_code(row["iscd"])
            held = whole(row["rmnd_qty"])
            sellable = max(
                held - committed.get(code, 0), 0
            )  # shares already offered in an open sell cannot be sold again
            positions.append(
                Position(
                    symbol=self._kr_code(code),
                    name=str(row.get("isnm") or ""),
                    quantity=held,
                    sellable=sellable,
                    average_price=decimal(row["byng_unpr"]),
                    last=decimal(row["psnt_prc"]),
                    market_value=decimal(row["vltn_amt"]),
                )
            )
        return positions

    def _us_positions(self) -> list[Position]:
        rows = self._paged(
            "/accounts/v1/me/overseas/holdings",
            {},
            ("iscd", "rmnd_clcd", "loan_date", "loan_dtl_type_code", "mtrt_date"),
        )
        positions = []
        for row in rows:
            if str(row.get("rmnd_clcd")) != "1":  # 소수·대출 잔고는 이 어댑터의 정수 주문 경로 밖
                continue
            held = whole(row["rmnd_dqty"])
            positions.append(
                Position(
                    symbol=str(row["ovrs_stck_symb_iscd"]),
                    name=str(row.get("isnm") or ""),
                    market="US",
                    quantity=held,
                    sellable=min(whole(row["seln_able_dqty"]), held),
                    average_price=decimal(row["avrg_unpr"]),
                    last=decimal(row["psnt_prc"]),
                    market_value=decimal(row["frcr_vltn_amt"]),
                )
            )
        return positions

    def get_positions(self) -> list[Position]:
        return self._call(self._positions)

    def get_balance(self) -> Balance:
        def read() -> Balance:
            value = sum((position.market_value for position in self._positions()), Decimal(0))
            return Balance(
                currency="USD" if self.market == "US" else "KRW", cash_available=self._cash(), position_value=value
            )

        return self._call(read)

    # ------------------------------------------------------------------ order list
    def _today(self) -> date:
        return self._clock().astimezone(KST).date()

    @staticmethod
    def _quantity(value: Any) -> int:
        return whole(value)

    def _kr_orders(self, code: str | None = None) -> list[Order]:
        params = {
            "oder_date": self._today().strftime("%Y%m%d"),
            "deal_cls_code": "0",
            "cntg_cls_code": "0",
            "sort_stnd_code": "2",
        }
        if code is not None:
            params["iscd"] = "A" + code
        rows = self._paged("/trading/v1/orders/history", params)
        originals = [row for row in rows if str(row.get("rctf_cncl_cls_code")) == "1"]
        touched: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            if str(row.get("rctf_cncl_cls_code")) in {"2", "3"}:
                touched.setdefault(_order_number(row["orgl_oder_no"]), []).append(row)
        orders = []
        for row in originals:
            number = _order_number(row["oder_no"])
            quantity, filled = whole(row["odqt"]), whole(row["total_cntg_qty"])
            cancelled = 0
            modified = False
            for other in touched.get(number, []):
                if str(other.get("oder_pros_stat_cls_code")) == "3":
                    continue  # a refused cancel/modify changed nothing
                if str(other["rctf_cncl_cls_code"]) == "3":
                    cancelled += whole(other["cncl_qty"])
                else:
                    modified = True
            status, remaining = self._derive(
                rejected=str(row.get("oder_pros_stat_cls_code")) == "3",
                quantity=quantity,
                filled=filled,
                cancelled=cancelled,
                venue_remaining=whole(row["ncnt_qty"]),
                touched=bool(touched.get(number)),
            )
            if modified:
                # Official corrections: KR changes all point at the FIRST order, so how a modify and a later cancel
                # split the quantity is not shown by the ledger. Unknown, never a guess.
                status, remaining = "unknown", None
            orders.append(
                Order(
                    id=number,
                    symbol=self._kr_code(_symbol_code(row["iscd"])),
                    side=_side(row["deal_cls_code"], "1", "2"),
                    order_type="limit" if str(row.get("oder_cls_code")) == KR_LIMIT else None,
                    quantity=quantity,
                    price=optional_price(row.get("oder_unpr")),
                    filled=filled,
                    remaining=remaining,
                    status=status,
                    fill_price=optional_price(row.get("cntg_unpr")) if filled else None,
                )
            )
        return orders

    @staticmethod
    def _derive(
        *, rejected: bool, quantity: int, filled: int, cancelled: int, venue_remaining: int, touched: bool
    ) -> tuple[OrderStatus, int]:
        """Status from the order row plus the cancel/modify rows that point at it. Numbers that do not add up are
        unknown: absence of evidence never proves a fill or a cancel."""
        if rejected:
            return "rejected", 0
        left = quantity - filled - cancelled
        if left < 0:
            return "unknown", 0
        if filled == quantity and not cancelled:
            return "filled", 0
        if left == 0:
            return ("cancelled" if cancelled else "filled"), 0
        if not touched and venue_remaining != left:
            return "unknown", left  # the venue's own remainder disagrees with the arithmetic
        return ("partial" if filled else "open"), left

    def _open_sell_quantity(self) -> dict[str, int]:
        committed: dict[str, int] = {}
        for order in self._kr_orders():
            if order.side == "sell" and order.status in {"open", "partial", "accepted", "unknown"}:
                committed[order.symbol] = committed.get(order.symbol, 0) + int(order.remaining or order.quantity or 0)
        return committed

    def _us_orders(self) -> list[Order]:
        today = self._today()
        params = {
            "oder_sta_date": (today - timedelta(days=3)).strftime("%Y%m%d"),
            "oder_fin_date": (today + timedelta(days=1)).strftime("%Y%m%d"),
            "seln_byov_clcd": "0",
            "cntg_cls_code": "0",
        }
        rows = self._paged("/trading/v1/overseas/orders/history", params)
        # A US modify is itself an order with its own number (the next change must name it); a cancel is not.
        originals = [row for row in rows if str(row.get("oder_rctf_cncl_cls_code")) in {"0", "1"}]
        touched: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            if str(row.get("oder_rctf_cncl_cls_code")) in {"1", "2"}:
                touched.setdefault(_order_number(row["orgl_oder_no"]), []).append(row)
        orders = []
        for row in originals:
            number = _order_number(row["oder_no"])
            quantity, filled = whole(row["oder_dqty"]), whole(row["cntg_dqty"])
            cancelled = 0
            modified = False
            for other in touched.get(number, []):
                if str(other.get("ovrs_stck_oder_stat_cls_code")) in {"1", "2", "4"}:
                    continue
                if str(other.get("oder_rctf_cncl_cls_code")) == "1":
                    modified = True
                else:
                    cancelled += whole(other["oder_dqty"])
            stat = str(row.get("ovrs_stck_oder_stat_cls_code"))
            status, remaining = self._derive(
                rejected=stat in {"1", "2"},
                quantity=quantity,
                filled=filled,
                cancelled=cancelled,
                venue_remaining=quantity - filled,
                touched=bool(touched.get(number)),
            )
            if stat == "3" and status in {"open", "partial"}:
                status, remaining = "cancelled", 0  # 자동취소(체결후): the rest was cancelled by the venue
            if modified:
                status, remaining = "unknown", None  # superseded by its replacement: that number carries the order
            orders.append(
                Order(
                    id=number,
                    symbol=str(row["iscd"]).strip().split(".")[0],
                    side=_side(row["seln_byov_clcd"], "1", "2"),
                    order_type="limit" if str(row.get("aspr_type_code")) == "2" else None,
                    quantity=quantity,
                    price=optional_price(row.get("ovrs_stck_oder_prc")),
                    filled=filled,
                    remaining=remaining,
                    status=status,
                    market="US",
                    fill_price=optional_price(row.get("cntg_crnc_prc")) if filled else None,
                )
            )
        return orders

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if self.market == "US":
            orders = self._call(self._us_orders)
            return [o for o in orders if symbol is None or o.symbol == symbol]
        code = self._symbol(symbol, "KR") if symbol is not None else None
        return self._call(lambda: self._kr_orders(code))

    def status(self, order: Order) -> Order:
        """The order list is the only authority. An order not listed exactly once is unknown, never filled/cancelled."""
        found = [candidate for candidate in self.list_orders(order.symbol) if candidate.id == order.id]
        if len(found) != 1:
            return order.model_copy(update={"status": "unknown"})
        got = found[0]
        return order.model_copy(
            update={
                "status": got.status,
                "filled": got.filled,
                "remaining": got.remaining,
                "fill_price": got.fill_price,
            }
        )

    # ------------------------------------------------------------------ writes
    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
        if isinstance(request, OrderRequest) and request.order_type != "limit":
            raise NotSupportedError("Meritz implements limit orders only")
        if isinstance(request, CancelRequest) and not _ORDER_NO.fullmatch(request.order_id):
            raise ConfigurationError("Meritz order number format is invalid")
        if isinstance(request, OrderRequest) and self.market == "US":
            if request.price != request.price.quantize(Decimal("0.0001")):
                raise ConfigurationError("Meritz US prices carry at most 4 decimal places")

    def _write(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """One POST, never repeated. A refusal is an explicit code; anything unreadable is unknown (see _call)."""
        self._require_writes()
        answer = self._send("POST", path, body=body, write=True)
        data = answer.get("data")
        if not isinstance(data, Mapping):
            raise InvalidResponseError("Meritz write answer has no data; reconcile before retrying")
        # `_judge` already refused any warning, but an answer that omits the field is not a proven acceptance.
        if data.get("warn_cls_code") != "0":
            raise InvalidResponseError("Meritz write answer lacks warn_cls_code; reconcile before retrying")
        if answer["rsp_cd"] != "0001":
            raise InvalidResponseError("Meritz write answer is not an order acceptance; reconcile before retrying")
        return data

    def _place_order(self, request: OrderRequest) -> Order:
        code = self._symbol(request.symbol, request.market)
        buy = request.side == "buy"
        if self.market == "US":
            _, vendor = self._us_codes()
            body = {
                "ovrs_stck_symb_iscd": code,
                "ovrs_exch_vndr_idno": vendor,
                "oder_cls_code": US_LIMIT,
                "oder_cond_cls_code": "0",
                "odqt": request.quantity,
                "ovrs_stck_oder_prc": format(request.price, "f"),
                "warn_cnfr_yn": "N",
            }
            path = f"/trading/v1/overseas/orders/{'buy' if buy else 'sell'}"
        else:
            body = {
                "iscd": "A" + code,
                "oder_unpr": int(request.price),
                "odqt": request.quantity,
                "oder_cls_code": KR_LIMIT,
                "oder_cond_cls_code": "0",
                "orgl_oder_no": 0,
                "whol_rctf_cncl_yn": "N",
                "warn_cnfr_yn": "N",
                "exch_kind_code": KR_KRX,
            }
            path = f"/trading/v1/orders/{'buy' if buy else 'sell'}"
        data = self._write(path, body)
        # 접수 is not 체결: the acknowledgement says nothing about fills.
        return Order(
            id=_order_number(data.get("oder_no")),
            symbol=code,
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            market=self.market,
        )

    def _original(self, order_id: str, code: str) -> Mapping[str, Any]:
        """The US cancel/modify body needs the original's order date, which only the order list knows."""
        today = self._today()
        params = {
            "oder_sta_date": (today - timedelta(days=3)).strftime("%Y%m%d"),
            "oder_fin_date": (today + timedelta(days=1)).strftime("%Y%m%d"),
            "seln_byov_clcd": "0",
            "cntg_cls_code": "0",
        }
        listed = self._paged("/trading/v1/overseas/orders/history", params)
        rows = [
            row
            for row in listed
            if str(row.get("oder_rctf_cncl_cls_code")) in {"0", "1"}
            and _order_number(row["oder_no"]) == order_id
            and str(row["iscd"]).strip().split(".")[0] == code
        ]
        # Official corrections: a US change must name the LATEST modify's number. A number that a live modify row
        # already points at is stale: sending it would act on the wrong order.
        superseded = any(
            str(row.get("oder_rctf_cncl_cls_code")) == "1"
            and str(row.get("ovrs_stck_oder_stat_cls_code")) not in {"1", "2", "4"}
            and _order_number(row["orgl_oder_no"]) == order_id
            for row in listed
        )
        if len(rows) != 1 or superseded:
            raise BrokerRejectedError(
                "Meritz order not found exactly once", code="order_not_found", kind="rejected", reason="order_not_found"
            )
        return rows[0]

    def _cancel_order(self, request: CancelRequest) -> Order:
        self._require_writes()
        code = self._symbol(request.symbol, request.market)
        if self.market == "US":
            original = self._original(request.order_id, code)  # a read: it may be repeated, the write below may not
            body = {
                "oder_date": str(original["oder_date"]),
                "orgl_oder_no": int(request.order_id),
                "oder_cls_code": US_LIMIT,
                "odqt": request.quantity,
                "warn_cnfr_yn": "N",
            }
            self._write("/trading/v1/overseas/orders/cancel", body)
        else:
            body = {
                "iscd": "A" + code,
                "oder_unpr": 0,
                "odqt": request.quantity,
                "oder_cond_cls_code": "0",
                "orgl_oder_no": int(request.order_id),
                "whol_rctf_cncl_yn": "N",
                "warn_cnfr_yn": "N",
                "exch_kind_code": KR_KRX,
            }
            self._write("/trading/v1/orders/cancel", body)
        # A cancel acknowledgement does not establish the final state of the order.
        return Order(id=request.order_id, symbol=code, status="accepted", market=self.market)

    def modify_order(self, order_id: str, request: OrderRequest) -> Order:
        """정정: `request` carries the new price/quantity (and the original side and symbol). It runs through the same
        OrderSafety path as a new order, so its full value is reserved and its client_key makes it once-only.
        The returned order has the NEW order number: the venue numbers a modify as a fresh order."""
        if not isinstance(order_id, str) or not _ORDER_NO.fullmatch(order_id):
            raise ConfigurationError("Meritz order number format is invalid")
        request = OrderRequest.model_validate(request.model_dump())
        self._validate_order(request)
        return self.safety.execute(
            self.broker_code, self._account, request, lambda: self._call(lambda: self._modify(order_id, request))
        )

    def _modify(self, order_id: str, request: OrderRequest) -> Order:
        code = self._symbol(request.symbol, request.market)
        if self.market == "US":
            original = self._original(order_id, code)
            body = {
                "oder_date": str(original["oder_date"]),
                "orgl_oder_no": int(order_id),
                "oder_cls_code": US_LIMIT,
                "odqt": request.quantity,
                "ovrs_stck_oder_prc": format(request.price, "f"),
                "oder_cond_cls_code": "0",
                "warn_cnfr_yn": "N",
            }
            data = self._write("/trading/v1/overseas/orders/modify", body)
        else:
            body = {
                "iscd": "A" + code,
                "oder_unpr": int(request.price),
                "odqt": request.quantity,
                "oder_cls_code": KR_LIMIT,
                "oder_cond_cls_code": "0",
                "orgl_oder_no": int(order_id),
                "whol_rctf_cncl_yn": "N",
                "warn_cnfr_yn": "N",
                "exch_kind_code": KR_KRX,
            }
            data = self._write("/trading/v1/orders/modify", body)
        return Order(
            id=_order_number(data.get("oder_no")),
            symbol=code,
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            market=self.market,
        )


def _side(value: Any, sell: str, buy: str) -> Side | None:
    text = str(value)
    return "sell" if text == sell else "buy" if text == buy else None


class _TokenExpired(Exception):
    """Internal: a read hit EGW0012x; the caller re-issues once."""


class _RateLimited(Exception):
    """Internal: a read hit EGW00200; the caller waits briefly."""
