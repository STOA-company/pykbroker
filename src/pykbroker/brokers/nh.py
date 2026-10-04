"""NH PLUG KR/US reads and guarded cash limit orders.

Specifications: https://www.nhplug.com/openapi-docs/common/openapi.json and
https://www.nhplug.com/openapi-docs/krstock/openapi.json and
https://www.nhplug.com/openapi-docs/gbstock/openapi.json (read 2026-10-04).
The caller owns durable token storage; ``token_cache`` is an injectable shared
mapping and ``token_state`` can be persisted by the caller. No global secret or
token file is created by this adapter.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SafetyError,
    TransportError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import KST, equity_rules
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price, whole

NH_API_BASE_URL = "https://api.nhplug.com:8443"
NH_MOCK_BASE_URL = "https://moapi.nhplug.com:8443"
NH_SUCCESS_CODES = frozenset({"00000", "00166", "00221", "13578"})
NH_CONTINUATION_CODES = frozenset({"00165", "00218"})
TOKEN_MARGIN = timedelta(minutes=5)
NH_READ_RATES = {"read": 4.0}
NH_MAX_BALANCE_PAGES = 100
_OPTIONS = {"app_key", "app_secret", "token", "token_type", "expires_at", "acct_type"}
_VENUES = frozenset({"KRX", "NXT", "UNT"})
_COMPLETED = re.compile(
    r"(?:^|\s)(?:조회\s*)?완료(?:되었습니다|됐습니다|했습니다|됨)?[.!\s]*$|"
    r"(?:^|\s)정상\s*처리되었습니다[.!\s]*$"
)


class _NhRestClient:
    """Small injectable HTTP boundary; HTTP errors never expose response bodies."""

    def __init__(self, *, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(
        self, path: str, *, headers: Mapping[str, str], data: bytes
    ) -> tuple[int, Mapping[str, Any], Mapping[str, str]]:
        request = urllib.request.Request(self.base_url + path, data=data, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, _json_object(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise TransportError("NH redirect refused") from None
            # A 4xx/5xx body may contain customer or credential data. Classify by status only.
            return exc.code, {}, dict(exc.headers)
        except Exception:
            raise TransportError("NH transport failed") from None


def _json_object(raw: bytes) -> Mapping[str, Any]:
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidResponseError("NH response is not JSON") from None
    if not isinstance(body, dict):
        raise InvalidResponseError("NH response is not an object")
    return body


def _expiry(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError("expires_at must be an ISO timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ConfigurationError("expires_at must be an ISO timestamp") from None
    if result.tzinfo is None:
        raise ConfigurationError("expires_at must have a timezone")
    return result.astimezone(UTC)


def _positive(value: Any, name: str) -> Decimal:
    number = decimal(value)
    if not number:
        raise InvalidResponseError(f"NH {name} is zero")
    return number


def _cts_flag(headers: Mapping[str, Any]) -> str | None:
    raw = headers.get("cts_flag")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidResponseError("NH continuation flag is invalid")
    flag = raw.strip().upper()
    if flag not in ("", "Y", "N"):
        raise InvalidResponseError("NH continuation flag is invalid")
    return flag or None


def _order_number(value: Any, *, max_digits: int = 10) -> str:
    if type(value) not in (int, str):
        raise InvalidResponseError("NH order number is invalid; reconcile before retrying")
    number = str(value).strip()
    if not number.isascii() or not number.isdigit() or len(number) > max_digits or not int(number):
        raise InvalidResponseError("NH order number is invalid; reconcile before retrying")
    return str(int(number))


class NH(BaseBroker):
    broker_code = "nh"
    supports_virtual = True

    def __init__(
        self,
        *,
        account: str,
        client_options: Mapping[str, Any],
        market: str = "KR",
        venue: str = "KRX",
        virtual: bool = False,
        allow_live_orders: bool = False,
        is_integrated: bool = False,
        client_factory: Callable[..., Any] | None = None,
        token_cache: MutableMapping[str, dict[str, str]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rate_limiter: RateLimiter | None = None,
        safety: OrderSafety | None = None,
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(account=account, market=market, virtual=virtual, safety=safety, halt_provider=halt_provider)
        if venue not in (_VENUES if market == "KR" else {"US"}):
            raise ConfigurationError("NH venue must be KRX, NXT or UNT for KR, US for US")
        if type(is_integrated) is not bool:
            raise ConfigurationError("is_integrated must be a boolean")
        self.is_integrated = is_integrated
        if type(allow_live_orders) is not bool:
            raise ConfigurationError("allow_live_orders must be a boolean")
        self._allow_live_orders = allow_live_orders
        options = dict(client_options)
        if not set(options) <= _OPTIONS:
            raise ConfigurationError("Unknown NH client option")
        app_key, app_secret, token = (options.get(k) for k in ("app_key", "app_secret", "token"))
        if not isinstance(app_key, str) or not app_key or not (token or app_secret):
            raise ConfigurationError("Supply app_key and a stored token or app_secret")
        if app_secret is not None and (not isinstance(app_secret, str) or not app_secret):
            raise ConfigurationError("app_secret must be a nonempty string")
        if token is not None and (not isinstance(token, str) or not token):
            raise ConfigurationError("token must be a nonempty string")
        if str(options.get("token_type", "Bearer")).lower() != "bearer":
            raise ConfigurationError("NH token_type must be Bearer")
        account_type = options.get("acct_type")
        if virtual and account_type != "03":
            raise ConfigurationError("NH mock account type must be explicitly 03")
        if account_type not in ({"03"} if virtual else {"01", "02"}):
            raise ConfigurationError("NH account type does not match the selected host")
        self.venue = venue
        self._app_key = app_key
        self._app_secret = app_secret
        self._token = token
        self._expires_at = _expiry(options.get("expires_at"))
        self._cache = token_cache
        self._clock = clock
        self._limiter = rate_limiter or credential_rate_limiter("nh", app_key, NH_READ_RATES)
        factory = client_factory or _NhRestClient
        order_host = NH_MOCK_BASE_URL if virtual else NH_API_BASE_URL
        self._client = factory(base_url=order_host)
        self._verify_host(self._client, order_host)
        self._order_host = order_host
        # Token issuance is always on the live host, including for mock accounts.
        self._auth_client = factory(base_url=NH_API_BASE_URL)
        self._verify_host(self._auth_client, NH_API_BASE_URL)
        self._publish_token_if_fresher()

    @property
    def virtual(self) -> bool:
        """Account mode follows the verified order host and cannot be reassigned."""
        return self._order_host == NH_MOCK_BASE_URL

    @staticmethod
    def _verify_host(client: Any, expected: str) -> None:
        if getattr(client, "base_url", None) != expected:
            raise ConfigurationError("NH client host does not match the requested mode")

    def token_state(self) -> dict[str, str]:
        """Return only the issued token and expiry for caller controlled persistence."""
        return {"token": self._token or "", "expires_at": self._expires_at.isoformat() if self._expires_at else ""}

    def _post_raw(
        self, client: Any, path: str, headers: Mapping[str, str], data: bytes
    ) -> tuple[int, Mapping[str, Any], Mapping[str, str]]:
        try:
            reply = client.post(path, headers=headers, data=data)
        except BrokerError:
            raise
        except Exception:
            raise TransportError("NH transport failed") from None
        if not (
            isinstance(reply, tuple)
            and len(reply) == 3
            and type(reply[0]) is int
            and isinstance(reply[1], Mapping)
            and isinstance(reply[2], Mapping)
        ):
            raise InvalidResponseError("NH transport response is invalid")
        return reply

    def _token_valid(self) -> bool:
        return bool(self._token) and (self._expires_at is None or self._clock() < self._expires_at - TOKEN_MARGIN)

    def _cached_token(self) -> tuple[str, datetime] | None:
        if self._cache is not None:
            cached = self._cache.get(self._app_key)
            if isinstance(cached, Mapping):
                token = cached.get("token")
                try:
                    expiry = _expiry(cached.get("expires_at")) if cached.get("expires_at") else None
                except ConfigurationError:
                    return None
                if isinstance(token, str) and token and expiry and self._clock() < expiry - TOKEN_MARGIN:
                    return token, expiry
        return None

    def _publish_token_if_fresher(self) -> None:
        if self._cache is None or not self._token_valid() or self._expires_at is None:
            return
        cached = self._cached_token()
        if cached is None or self._expires_at > cached[1]:
            self._cache[self._app_key] = self.token_state()

    def _ensure_token(self) -> str:
        cached = self._cached_token()
        if cached is not None and (not self._token_valid() or self._expires_at is None or cached[1] > self._expires_at):
            self._token, self._expires_at = cached
            return cached[0]
        if self._token_valid():
            self._publish_token_if_fresher()
            return self._token  # type: ignore[return-value]
        if not self._app_secret:
            raise ConfigurationError("NH token is absent or expired; supply app_secret or a fresh stored token")
        return self.issue_token()["token"]

    def _discard_token(self) -> None:
        rejected_token = self._token
        self._token = None
        self._expires_at = None
        if self._cache is not None:
            cached = self._cache.get(self._app_key)
            if isinstance(cached, Mapping) and cached.get("token") == rejected_token:
                self._cache.pop(self._app_key, None)

    def issue_token(self) -> dict[str, str]:
        """Explicit or lazy issuance; never retry on 429 and never include secrets in errors."""
        if not self._app_secret:
            raise ConfigurationError("NH app_secret is required to issue a token")
        form = urllib.parse.urlencode(
            {
                "appkey": self._app_key,
                "appsecretkey": self._app_secret,
                "grant_type": "client_credentials",
                "scope": "oob",
            }
        ).encode("utf-8")
        status, body, _ = self._post_raw(
            self._auth_client,
            "/oauth2/token",
            {"content-type": "application/x-www-form-urlencoded"},
            form,
        )
        if status in (400, 401):
            raise ConfigurationError("NH token credentials were rejected")
        if status == 429:
            raise TransportError("NH token rate limit")
        if status != 200:
            raise TransportError(f"NH token HTTP {status}")
        token, token_type, ttl = (
            body.get("access_token"),
            body.get("token_type", "Bearer"),
            body.get("expires_in", 86400),
        )
        if (
            not isinstance(token, str)
            or not token
            or str(token_type).lower() != "bearer"
            or type(ttl) is not int
            or ttl <= 0
        ):
            raise InvalidResponseError("NH token response is invalid")
        self._token = token
        self._expires_at = self._clock() + timedelta(seconds=ttl)
        state = self.token_state()
        self._publish_token_if_fresher()
        return state

    def _read_response(
        self,
        path: str,
        payload: Mapping[str, Any],
        expected: str | None,
        *,
        client: Any | None = None,
        cts: str | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        self._ensure_token()
        headers = {
            "authorization": f"Bearer {self._token}",
            "x-client-id": self._app_key,
            "content-type": "application/json; charset=UTF-8",
        }
        if cts is not None:
            headers.update({"cts": cts, "cts_flag": "Y"})
        data = json.dumps({"Input_0": payload}, ensure_ascii=False).encode("utf-8")
        self._limiter.wait("read")
        status, body, response_headers = self._post_raw(client or self._client, path, headers, data)
        if status == 401 or (status == 200 and str(body.get("rsp_cd", "")).strip() == "IGW40043"):
            # HTTP 401 and this business code both mean an invalid access token.
            self._discard_token()
            self._ensure_token()
            headers["authorization"] = f"Bearer {self._token}"
            self._limiter.wait("read")
            status, body, response_headers = self._post_raw(client or self._client, path, headers, data)
        if status == 401 or (status == 200 and str(body.get("rsp_cd", "")).strip() == "IGW40043"):
            self._discard_token()
            raise ConfigurationError("NH token was rejected")
        if status == 429:
            raise TransportError("NH read rate limit")
        if status != 200:
            raise TransportError(f"NH read HTTP {status}")
        code = str(body.get("rsp_cd", "")).strip()
        nested = body.get("message")
        if nested is not None and not isinstance(nested, Mapping):
            raise InvalidResponseError("NH response message is malformed")
        message = body.get("rsp_msg") or (nested.get("usr_msg") if nested is not None else None)
        if message is not None and not isinstance(message, str):
            raise InvalidResponseError("NH response message is malformed")
        if code not in NH_SUCCESS_CODES | NH_CONTINUATION_CODES:
            raise BrokerRejectedError("NH read rejected")
        if message and re.search(r"완료되지|미완료|불완료|실패|오류|에러", message):
            raise BrokerRejectedError("NH read rejected")
        # Documented blocks are optional in the wire schema, but a money/price read
        # without the expected block cannot be interpreted as zero or success.
        if expected is not None and (expected not in body or body[expected] is None):
            raise InvalidResponseError(f"NH read missing {expected}")
        return body, response_headers

    def _read(
        self, path: str, payload: Mapping[str, Any], expected: str | None, *, client: Any | None = None
    ) -> Mapping[str, Any]:
        body, headers = self._read_response(path, payload, expected, client=client)
        page_headers = {str(key).lower(): value for key, value in headers.items()}
        if _cts_flag(page_headers) not in (None, "N") or str(body.get("rsp_cd", "")).strip() in NH_CONTINUATION_CODES:
            raise InvalidResponseError("NH single-result read unexpectedly needs continuation")
        return body

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            supports_limit_order=self.venue in {"KRX", "NXT", "US"},
            supports_market_order=False,
            supports_single_order_cancel=self.venue in {"KRX", "NXT", "US"},
            supports_order_history=True,
            supports_virtual_account=True,
            virtual=self.virtual,
            venues=(self.venue,),
            markets=(self.market,),
            supports_nxt=self.venue == "NXT",
        )

    def verify_account(self) -> str:
        """Check /n2/acctinfo account ownership and its documented live/mock type."""

        def read() -> str:
            body = self._read("/n2/acctinfo", {}, "Output_0", client=self._auth_client)
            rows = body["Output_0"]
            if isinstance(rows, Mapping):
                rows = [rows]
            if not isinstance(rows, list):
                raise InvalidResponseError("NH account list is invalid")
            expected = {"03"} if self.virtual else {"01", "02"}
            for row in rows:
                if not isinstance(row, Mapping):
                    raise InvalidResponseError("NH account row is invalid")
                if row.get("acct_no") == self._account:
                    if row.get("acct_type") not in expected:
                        raise ConfigurationError("NH account type does not match the selected host")
                    return str(row["acct_type"])
            raise ConfigurationError("NH account is absent from the credential's account list")

        return self._call(read)

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        code = self._symbol(symbol, market)
        if self.market == "US":
            return self._call(lambda: self._us_quote(code))

        def read() -> Quote:
            row = self._quote_row(code)
            bid = decimal(row["bidp"]) if row.get("bidp") not in (None, "", 0, "0") else None
            ask = decimal(row["askp"]) if row.get("askp") not in (None, "", 0, "0") else None
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None
            prev = decimal(row["stck_prdy_clpr"]) if row.get("stck_prdy_clpr") not in (None, "", 0, "0") else None
            return Quote(symbol=code, last=_positive(row["stck_prpr"], "last price"), bid=bid, ask=ask, prev_close=prev)

        return self._call(read)

    def _quote_row(self, code: str) -> Mapping[str, Any]:
        row = self._read(
            "/krstock/quote/v1/currentPrice",
            {"market_cd": self.venue, "iem_cd": code},
            "Output_0",
            client=self._auth_client,
        )["Output_0"]
        if not isinstance(row, Mapping):
            raise InvalidResponseError("NH quote block is invalid")
        if row.get("iem_cd") is not None and row["iem_cd"] != code:
            raise InvalidResponseError("NH quote symbol does not match request")
        return row

    def get_rules(
        self, symbol: str, *, now: datetime | None = None, trading_day: Callable[[date], bool] | None = None
    ) -> Rules:
        if self.market == "US":
            code = self._symbol(symbol, "US")
            rules = super().get_rules(code, now=now, trading_day=trading_day)
            # Master trade_flag has no documented normal enum; caller False is not native proof.
            return rules.model_copy(update={"halted": True if rules.halted is True else None})
        code = self._symbol(symbol, "KR")

        def read() -> Rules:
            row = self._quote_row(code)
            last = _positive(row["stck_prpr"], "last price")
            # Official currentPrice Output_0: stck_mxpr=상한가, stck_llam=하한가.
            # Missing/zero sides stay unknown; never derive a band from the previous close.
            upper, lower = optional_price(row.get("stck_mxpr")), optional_price(row.get("stck_llam"))
            previous = optional_price(row.get("stck_prdy_clpr"))
            if upper is None or lower is None:
                upper = lower = None
            elif not lower <= last <= upper or (previous is not None and not lower <= previous <= upper):
                raise InvalidResponseError("NH daily band does not contain the reference and last prices")
            return equity_rules(
                code,
                last,
                halted=self._halt_from_provider(code),
                now=now,
                trading_day=trading_day,
                limit_up=upper,
                limit_down=lower,
            )

        return self._call(read)

    def _balance(self, *, allow_empty_positions: bool = False) -> Mapping[str, Any]:
        payload = {
            "act_no": self._account,
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "1",
            "qut_dit_cd": self.venue,
            "aly_qut_cd": "2",
        }
        rows: list[Any] = []
        seen: set[str] = set()
        cts: str | None = None
        for _ in range(NH_MAX_BALANCE_PAGES):
            body, headers = self._read_response("/krstock/inquiry/v1/balance", payload, None, cts=cts)
            page_rows = body.get("Output_1", [])
            if not isinstance(page_rows, list):
                raise InvalidResponseError("NH positions block is invalid")
            rows.extend(page_rows)
            page_headers = {str(key).lower(): value for key, value in headers.items()}
            flag = _cts_flag(page_headers)
            next_cts = str(page_headers.get("cts", "")).strip()
            code = str(body.get("rsp_cd", "")).strip()
            if flag == "N" and code in NH_CONTINUATION_CODES:
                raise InvalidResponseError("NH balance continuation signals conflict")
            more = flag == "Y" or (flag is None and code in NH_CONTINUATION_CODES)
            if not more:
                if not isinstance(body.get("Output_0"), Mapping):
                    if allow_empty_positions and code == "13578" and not rows and body.get("Output_0") is None:
                        return {**body, "Output_1": []}
                    raise InvalidResponseError("NH balance final summary is missing")
                return {**body, "Output_1": rows}
            if not next_cts or next_cts in seen:
                raise InvalidResponseError("NH balance continuation key is missing or repeated")
            seen.add(next_cts)
            cts = next_cts
        raise InvalidResponseError("NH balance exceeded the page limit")

    def get_balance(self) -> Balance:
        if self.market == "US":
            return self._call(self._us_balance)

        def read() -> Balance:
            row = self._balance()["Output_0"]
            if not isinstance(row, Mapping):
                raise InvalidResponseError("NH balance block is invalid")
            # Never count margin or unsettled assets as spendable cash.
            cash = min(decimal(row["dca"]), decimal(row["orr_pbl_amt"]))
            return Balance(cash_available=cash, position_value=decimal(row["tot_eal_amt"]))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        if self.market == "US":
            return self._call(self._us_positions)

        def read() -> list[Position]:
            body = self._balance(allow_empty_positions=True)
            rows = body.get("Output_1", [])
            if not isinstance(rows, list):
                raise InvalidResponseError("NH positions block is invalid")
            grouped: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    raise InvalidResponseError("NH position row is invalid")
                code = self._symbol(row["iem_cd"], "KR")
                quantity = whole(row["itg_bnc_qty"])
                price, last, value = decimal(row["phs_pr"]), decimal(row["now_pr"]), decimal(row["eal_amt"])
                if code not in grouped:
                    grouped[code] = {
                        "name": str(row.get("iem_nm", "")),
                        "quantity": 0,
                        "cost": Decimal(0),
                        "last": last,
                        "value": Decimal(0),
                    }
                item = grouped[code]
                if item["last"] != last:
                    raise InvalidResponseError("NH position prices conflict for the same symbol")
                item["quantity"] += quantity
                item["cost"] += price * quantity
                item["value"] += value
            return [
                Position(
                    symbol=code,
                    name=item["name"],
                    quantity=item["quantity"],
                    sellable=self._sellable_quantity(code),
                    average_price=item["cost"] / item["quantity"] if item["quantity"] else Decimal(0),
                    last=item["last"],
                    market_value=item["value"],
                )
                for code, item in grouped.items()
            ]

        return self._call(read)

    def _sellable_quantity(self, code: str) -> int:
        body = self._read(
            "/krstock/inquiry/v1/sellableQuantity",
            {
                "act_no": self._account,
                "iem_cd": code,
                "cfd_lon_cd": "00",
            },
            None,
        )
        row = body.get("Output_0")
        if row is None and str(body.get("rsp_cd", "")).strip() == "13578":
            return 0
        if not isinstance(row, Mapping):
            raise InvalidResponseError("NH sellable quantity block is missing")
        try:
            response_code = self._symbol(row["iem_cd"], "KR")
        except (ConfigurationError, KeyError):
            raise InvalidResponseError("NH sellable quantity symbol is invalid") from None
        if response_code != code:
            raise InvalidResponseError("NH sellable quantity does not match symbol")
        return whole(row["sll_pbl_qty"])

    def available_qty(self, symbol: str) -> int:
        if self.market == "US":
            code = self._symbol(symbol, "US")
            return self._call(lambda: whole(self._us_buying_power(code, "3")["sll_pbl_qty"]))
        code = self._symbol(symbol, "KR")
        return self._call(lambda: self._sellable_quantity(code))

    def buyable_quantity(self, symbol: str, *, price: Decimal | None = None) -> int:
        """Cash buyable quantity at the caller's ordinary limit price."""
        if self.market == "US":
            code = self._symbol(symbol, "US")
            if price is None:
                raise ConfigurationError("NH buyable quantity requires a limit price")
            self._us_price(price)
            return self._call(lambda: whole(self._us_buying_power(code, "2", price)["orr_pbl_qty"]))
        code = self._symbol(symbol, "KR")
        if price is None:
            raise ConfigurationError("NH buyable quantity requires a limit price")
        payload: dict[str, Any] = {
            "ost_dit_cd": "1",
            "act_no": self._account,
            "iem_cd": code,
            "nmn_pr_tp_cd": "01",
        }
        value = decimal(price)
        if value <= 0 or value != value.to_integral_value():
            raise ConfigurationError("NH KR order price must be positive whole won")
        payload["orr_pr"] = int(value)

        def read() -> int:
            row = self._read("/krstock/inquiry/v1/buyableQuantity", payload, "Output_0")["Output_0"]
            if not isinstance(row, Mapping):
                raise InvalidResponseError("NH buying power block is invalid")
            if "csh_orr_pbl_qty" not in row:
                raise InvalidResponseError("NH cash buyable quantity is missing")
            return whole(row["csh_orr_pbl_qty"])

        return self._call(read)

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
        if self._order_host == NH_API_BASE_URL and self._allow_live_orders is not True:
            raise SafetyError("NH live orders are locked until order-to-ledger reconciliation is verified")
        if request.venue is not None and request.venue != self.venue:
            raise NotSupportedError("NH order venue must match the configured venue")
        if isinstance(request, OrderRequest):
            if request.order_type != "limit":
                raise NotSupportedError("NH implements cash limit orders only")
            # The shared stock ladder does not cover ETFs and other KR products.
            # Whole won is checked by BaseBroker; the venue decides product ticks.
            if self.market == "US":
                self._us_price(request.price)
        elif self.market == "US":
            if not request.order_id.startswith("us:"):
                raise ConfigurationError("NH US cancellation requires the original US order number")
            _order_number(request.order_id[3:])
        elif not request.order_id.startswith("mkt:"):
            raise ConfigurationError("NH cancellation requires the market order number from a placement")
        else:
            _order_number(request.order_id[4:])

    def _write(self, path: str, payload: Mapping[str, Any], *, number_field: str = "mkt_orr_no") -> Mapping[str, Any]:
        """One order POST. Token renewal and HTTP replay are forbidden after transmission."""
        token = self._ensure_token()  # Preflight only: token issuance here cannot duplicate an order.
        headers = {
            "authorization": f"Bearer {token}",
            "x-client-id": self._app_key,
            "content-type": "application/json; charset=UTF-8",
        }
        data = json.dumps({"Input_0": payload}, ensure_ascii=False).encode("utf-8")
        self._limiter.wait("read")
        status, body, _ = self._post_raw(self._client, path, headers, data)
        if status == 401 or (status == 200 and str(body.get("rsp_cd", "")).strip() == "IGW40043"):
            self._discard_token()
            raise BrokerRejectedError("NH order authorization rejected; no order replay occurred")
        if status != 200:
            raise TransportError("NH order outcome is unknown; reconcile before retrying")
        code = body.get("rsp_cd")
        if code is not None and not isinstance(code, str):
            raise InvalidResponseError("NH order code is invalid; reconcile before retrying")
        nested = body.get("message")
        if nested is not None and not isinstance(nested, Mapping):
            raise InvalidResponseError("NH order message is invalid; reconcile before retrying")
        message = body.get("rsp_msg") or (nested.get("usr_msg") if nested is not None else None)
        if message is not None and not isinstance(message, str):
            raise InvalidResponseError("NH order message is invalid; reconcile before retrying")
        completed = bool(message and _COMPLETED.search(message))
        # Only known success codes plus a valid market order number acknowledge a write.
        if code is None or code.strip() not in NH_SUCCESS_CODES:
            raise InvalidResponseError("NH order outcome is unknown; reconcile before retrying")
        if message and "완료" in message and not completed:
            raise InvalidResponseError("NH order outcome is unknown; reconcile before retrying")
        output = body.get("Output_0")
        if not isinstance(output, Mapping):
            raise InvalidResponseError("NH order acknowledgement is missing; reconcile before retrying")
        _order_number(output.get(number_field))
        return output

    def _place_order(self, request: OrderRequest) -> Order:
        if self.market == "US":
            return self._us_place_order(request)
        code = self._symbol(request.symbol, request.market)
        payload = {
            "act_no": self._account,
            "iem_cd": code,
            "orr_qty": request.quantity,
            "orr_pr": int(request.price),
            "orr_amt": int(request.price) * request.quantity,
            "nmn_pr_tp_cd": "01",
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": self.venue,
            "sor_mkt_sli_yn": "N",
        }
        endpoint = "cashBuy" if request.side == "buy" else "cashSell"
        output = self._write(f"/krstock/order/v1/{endpoint}", payload)
        return Order(
            id=f"mkt:{_order_number(output['mkt_orr_no'])}",
            symbol=code,
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self.virtual,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        if self.market == "US":
            return self._us_cancel_order(request)
        code = self._symbol(request.symbol, request.market)
        self._write(
            "/krstock/order/v1/cancel",
            {
                "act_no": self._account,
                "org_mkt_orr_no": int(_order_number(request.order_id[4:])),
                "all_pat_dit_cd": "2",
                "iem_cd": code,
                "cor_qty": request.quantity,
            },
        )
        # A cancellation acknowledgement does not prove the order's final state.
        return Order(id=request.order_id, symbol=code, status="accepted", virtual=self.virtual)

    def _daily_rows(self) -> list[Mapping[str, Any]]:
        payload = {
            "orr_dt": self._clock().astimezone(KST).strftime("%Y%m%d"),
            "act_no": self._account,
            "ost_cns_dit": "0",
            "orr_mkt_cd": "00",
        }
        rows: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        cts: str | None = None
        for _ in range(NH_MAX_BALANCE_PAGES):
            body, headers = self._read_response("/krstock/inquiry/v1/dailyOrderExecution", payload, None, cts=cts)
            # Current official schema uses Output_0; older fixtures/wire used Output_1.
            page = body.get("Output_1") if "Output_1" in body else body.get("Output_0")
            if page is None:
                if str(body.get("rsp_cd", "")).strip() != "13578":
                    raise InvalidResponseError("NH daily order rows are missing")
                page = []
            if not isinstance(page, list) or any(not isinstance(row, Mapping) for row in page):
                raise InvalidResponseError("NH daily order rows are invalid")
            rows.extend(page)
            page_headers = {str(key).lower(): value for key, value in headers.items()}
            flag = _cts_flag(page_headers)
            code = str(body.get("rsp_cd", "")).strip()
            if flag == "N" and code in NH_CONTINUATION_CODES:
                raise InvalidResponseError("NH daily order continuation signals conflict")
            more = flag == "Y" or (flag is None and code in NH_CONTINUATION_CODES)
            if not more:
                return rows
            next_cts = str(page_headers.get("cts", "")).strip()
            if not next_cts or next_cts in seen:
                raise InvalidResponseError("NH daily order continuation key is missing or repeated")
            seen.add(next_cts)
            cts = next_cts
        raise InvalidResponseError("NH daily order list exceeded the page limit")

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if self.market == "US":
            code = self._symbol(symbol, "US") if symbol is not None else None
            return self._call(lambda: self._us_orders(code))
        code = self._symbol(symbol, "KR") if symbol is not None else None

        def read() -> list[Order]:
            orders = []
            rows = self._daily_rows()
            touched = {
                _order_number(row[key], max_digits=11)
                for row in rows
                for key in ("org_itg_orr_no", "mo_itg_orr_no")
                if row.get(key) is not None and str(row[key]).strip("0 ")
            }
            for row in rows:
                listed = self._symbol(row["iem_cd"], "KR")
                if code is not None and listed != code:
                    continue
                integrated = _order_number(row["itg_orr_no"], max_digits=11)
                quantity = whole(row["orr_qty"])
                filled = whole(row["tot_cns_qty"])
                remaining = whole(row["ny_cns_qty"])
                cancelled = whole(row["can_qty"]) if row.get("can_qty") not in (None, "") else 0
                corrected = whole(row["cor_qty"]) if row.get("cor_qty") not in (None, "") else 0
                changed = bool(str(row.get("cor_can_dit_cd_nm") or "").strip())
                rejected = bool(str(row.get("orr_rjt_rsn_cd_nm") or "").strip())
                state = "unknown"
                if (
                    quantity > 0
                    and not (changed or rejected or cancelled or corrected or integrated in touched)
                    and filled <= quantity
                    and filled + remaining == quantity
                ):
                    if filled == quantity:
                        state = "filled"
                    elif remaining:
                        state = "partial" if filled else "open"
                side = {"매수": "buy", "매도": "sell"}.get(str(row.get("sby_dit_cd_nm", "")).strip())
                order_type = "limit" if str(row.get("nmn_pr_tp_cd_nm", "")).strip() in {"보통가", "지정가"} else None
                orders.append(
                    Order(
                        id=f"itg:{integrated}",
                        symbol=listed,
                        side=side,
                        order_type=order_type,
                        quantity=quantity,
                        price=decimal(row["orr_pr"]),
                        fill_price=optional_price(row.get("cns_avg_uit_pr")) if filled else None,
                        filled=filled,
                        remaining=remaining,
                        status=state,
                        virtual=self.virtual,
                    )
                )
            return orders

        return self._call(read)

    def status(self, order: Order) -> Order:
        code = self._symbol(order.symbol, order.market)
        if not order.id.startswith("us:" if self.market == "US" else "itg:"):
            # The API documents no relation between mkt_orr_no and itg_orr_no.
            return order.model_copy(update={"status": "unknown"})

        def read() -> Order:
            matches = [item for item in self.list_orders(code) if item.id == order.id]
            if len(matches) != 1:
                return order.model_copy(update={"status": "unknown"})
            return matches[0]

        return self._call(read)

    @staticmethod
    def _us_price(price: Any) -> Decimal:
        value = decimal(price)
        if value <= 0 or value * 100 != (value * 100).to_integral_value():
            raise ConfigurationError("NH US limit price must be positive with at most two decimal places")
        return value

    def _us_quote(self, code: str) -> Quote:
        row = self._read("/gbstock/quote/v1/current", {"iem_cd": code}, "Output_0", client=self._auth_client)[
            "Output_0"
        ]
        if not isinstance(row, Mapping) or row.get("iem_cd") != code:
            raise InvalidResponseError("NH US quote symbol does not match request")
        bid, ask = optional_price(row.get("best_bid1")), optional_price(row.get("best_ask1"))
        if bid is not None and ask is not None and bid > ask:
            bid = ask = None
        return Quote(
            symbol=code, market="US", last=_positive(row["trdprc"], "last price"), bid=bid, ask=ask,
            prev_close=optional_price(row.get("hst_trdprc"))
        )

    def _us_buying_power(self, code: str, kind: str, price: Decimal | None = None) -> Mapping[str, Any]:
        # 4 explicitly excludes integrated-margin buying power. 1 includes it,
        # and is selected only for an account already enrolled by its owner.
        payload = {
            "act_no": self._account,
            "pcs_dit": kind,
            "fc_sec_trd_nat_cd": "200",
            "iem_cd": code,
            "wtm_cur_knd_cd": "1" if self.is_integrated else "4",
            "oss_orr_knd_cd": "1",
            "ahi_nmn_pr_tp_cd": "00",
            "cfd_lon_cd": "00",
        }
        if price is not None:
            payload["fc_orr_uit_pr"] = float(self._us_price(price))
        row = self._read("/gbstock/inquiry/v1/buyableAmount", payload, "Output_0")["Output_0"]
        if not isinstance(row, Mapping):
            raise InvalidResponseError("NH US buying power block is invalid")
        if kind in {"1", "2"} and row.get("wtm_cur_cd") != "USD":
            raise InvalidResponseError("NH US buying power currency is not USD")
        return row

    def buyable_amount(self, symbol: str, *, price: Decimal | None = None) -> Decimal:
        """Broker-calculated USD buying power; account enrollment is never changed."""
        if self.market != "US":
            raise NotSupportedError("NH symbol buying-power amount is implemented for US")
        code = self._symbol(symbol, "US")
        return self._call(lambda: decimal(self._us_buying_power(code, "1", price)["orr_pbl_amt"]))

    def _us_pages(
        self, path: str, payload: Mapping[str, Any], block: str
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        rows, seen, cts = [], set(), None
        for _ in range(NH_MAX_BALANCE_PAGES):
            body, headers = self._read_response(path, payload, None, cts=cts)
            page = body.get(block)
            if page is None and str(body.get("rsp_cd", "")).strip() == "13578":
                page = []
            if not isinstance(page, list) or any(not isinstance(row, Mapping) for row in page):
                raise InvalidResponseError("NH US list block is missing or invalid")
            rows.extend(page)
            headers = {str(k).lower(): v for k, v in headers.items()}
            flag = _cts_flag(headers)
            continuing = str(body.get("rsp_cd", "")).strip() in NH_CONTINUATION_CODES
            if flag == "N" and continuing:
                raise InvalidResponseError("NH US continuation signals conflict")
            if flag != "Y" and not (flag is None and continuing):
                return body, rows
            cts = str(headers.get("cts", "")).strip()
            if not cts or cts in seen:
                raise InvalidResponseError("NH US continuation key is missing or repeated")
            seen.add(cts)
        raise InvalidResponseError("NH US list exceeded the page limit")

    def _us_holdings(self) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        body, rows = self._us_pages(
            "/gbstock/inquiry/v1/balance",
            {
                "act_no": self._account,
                "qut_iqr_dit_cd": "1",
                "fc_sec_trd_nat_cd": "200",
                "cur_cd": "USD",
                "xns_dit_cd": "0",
            },
            "Output_1",
        )
        summary = body.get("Output_0")
        if not isinstance(summary, Mapping):
            raise InvalidResponseError("NH US balance summary is missing")
        return summary, rows

    def _us_balance(self) -> Balance:
        summary, _ = self._us_holdings()
        # Account cash remains conservative. Symbol buying power is queried above;
        # this cash field does not add collateral, cross-currency margin or assets.
        _, rows = self._us_pages("/gbstock/inquiry/v1/margin", {"act_no": self._account}, "Output_0")
        dollars = [row for row in rows if row.get("cur_cd") == "USD"]
        if len(dollars) != 1:
            raise InvalidResponseError("NH USD cash row is absent or ambiguous")
        cash = min(decimal(dollars[0]["fc_dca"]), decimal(dollars[0]["fc_drn_pbl_amt"]))
        return Balance(currency="USD", cash_available=cash, position_value=decimal(summary["fc_eal_amt"]))

    def _us_positions(self) -> list[Position]:
        _, rows = self._us_holdings()
        positions, seen = [], set()
        for row in rows:
            if row.get("fc_sec_trd_nat_cd") != "200" or row.get("cur_cd") != "USD":
                raise InvalidResponseError("NH US holding country or currency does not match")
            code = self._symbol(row["iem_cd"], "US")
            if code in seen:
                raise InvalidResponseError("NH US holding symbol is ambiguous")
            seen.add(code)
            positions.append(
                Position(
                    symbol=code,
                    name=str(row.get("iem_nm", "")),
                    market="US",
                    quantity=whole(row["cns_bse_bnc_qty"]),
                    sellable=whole(row["sll_pbl_qty1"]),
                    average_price=decimal(row["fc_phs_uit_pr"]),
                    last=decimal(row["fc_sec_end_pr"]),
                    market_value=decimal(row["fc_eal_amt"]),
                )
            )
        return positions

    def _us_place_order(self, request: OrderRequest) -> Order:
        payload = {
            "act_no": self._account,
            "fc_sec_trd_nat_cd": "200",
            "iem_cd": request.symbol,
            "orr_qty": request.quantity,
            "fc_orr_uit_pr": float(self._us_price(request.price)),
            "ahi_nmn_pr_tp_cd": "00",
            "wtm_cur_knd_cd": "1",
        }
        output = self._write("/gbstock/order/v1/" + request.side, payload, number_field="orr_no")
        return Order(
            id="us:" + _order_number(output["orr_no"]),
            symbol=request.symbol,
            market="US",
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self.virtual,
        )

    def _us_cancel_order(self, request: CancelRequest) -> Order:
        self._write(
            "/gbstock/order/v1/cancel",
            {
                "act_no": self._account,
                "org_orr_no": int(_order_number(request.order_id[3:])),
                "fc_sec_trd_nat_cd": "200",
                "iem_cd": request.symbol,
                "can_qty": request.quantity,
                "all_pat_dit_cd": "2",
            },
            number_field="orr_no",
        )
        return Order(id=request.order_id, symbol=request.symbol, market="US", status="accepted", virtual=self.virtual)

    def _us_orders(self, code: str | None) -> list[Order]:
        payload = {
            "orr_dt": self._clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d"),
            "act_no": self._account,
            "oss_sby_dit_cd": "0",
            "sot_dit": "0",
            "ost_cns_dit": "0",
        }
        if code is not None:
            payload["iem_cd"] = code
        _, rows = self._us_pages("/gbstock/inquiry/v1/unexecuted", payload, "Output_0")
        touched = {_order_number(row["org_orr_no"]) for row in rows if row.get("org_orr_no") not in (None, "", 0, "0")}
        orders = []
        for row in rows:
            if row.get("fc_sec_trd_nat_cd") != "200":
                raise InvalidResponseError("NH US ledger country does not match")
            symbol = self._symbol(row["iem_cd"], "US")
            if code is not None and symbol != code:
                continue
            number = _order_number(row["orr_no"])
            quantity, filled, remaining = whole(row["orr_qty"]), whole(row["cns_qty"]), whole(row["ny_cns_orr_qty"])
            changed = any(
                str(row.get(k) or "").strip() for k in ("cor_can_dit_cd", "cor_can_dit_nm", "rjt_rsn_cts", "orr_sts_nm")
            )
            changed = changed or whole(row.get("can_qty") or 0) > 0 or whole(row.get("cor_qty") or 0) > 0
            state = "unknown"
            if quantity > 0 and filled + remaining == quantity and not changed and number not in touched:
                state = "filled" if filled == quantity else "partial" if filled else "open"
            orders.append(
                Order(
                    id="us:" + number,
                    symbol=symbol,
                    market="US",
                    side={"1": "sell", "2": "buy"}.get(str(row.get("oss_sby_dit_cd"))),
                    order_type="limit" if row.get("ahi_nmn_pr_tp_cd") == "00" else None,
                    quantity=quantity,
                    price=decimal(row["fc_orr_uit_pr"]),
                    filled=filled,
                    remaining=remaining,
                    # cns_pr is documented as execution price, not an order-average.
                    # Keep average fill price unknown until the vendor proves that binding.
                    fill_price=None,
                    status=state,
                    virtual=self.virtual,
                )
            )
        return orders
