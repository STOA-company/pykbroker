"""NH PLUG domestic reads. Not registered in the broker factory; no order writes.

Specifications: https://www.nhplug.com/openapi-docs/common/openapi.json and
https://www.nhplug.com/openapi-docs/krstock/openapi.json (read 2026-09-27).
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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import Balance, Capabilities, Position, Quote
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal, whole

NH_API_BASE_URL = "https://api.nhplug.com:8443"
NH_MOCK_BASE_URL = "https://moapi.nhplug.com:8443"
NH_SUCCESS_CODES = frozenset({"00000", "00166", "00221", "13578"})
NH_CONTINUATION_CODES = frozenset({"00165", "00218"})
TOKEN_MARGIN = timedelta(minutes=5)
NH_READ_RATES = {"read": 4.0}
NH_MAX_BALANCE_PAGES = 100
_OPTIONS = {"app_key", "app_secret", "token", "token_type", "expires_at", "acct_type"}
_VENUES = frozenset({"KRX", "NXT", "UNT"})


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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _json_object(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
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
        client_factory: Callable[..., Any] | None = None,
        token_cache: MutableMapping[str, dict[str, str]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(account=account, market=market, virtual=virtual)
        if market != "KR":
            raise NotSupportedError("NH overseas reads are not implemented")
        if venue not in _VENUES:
            raise ConfigurationError("NH venue must be KRX, NXT or UNT")
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
        if account_type is not None and account_type not in ({"03"} if virtual else {"01", "02"}):
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
        self._client = factory(base_url=NH_MOCK_BASE_URL if virtual else NH_API_BASE_URL)
        # Token issuance is always on the live host, including for mock accounts.
        self._auth_client = factory(base_url=NH_API_BASE_URL)
        if token_cache is not None and token and self._expires_at:
            token_cache[app_key] = self.token_state()

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

    def _ensure_token(self) -> str:
        if self._token_valid():
            return self._token  # type: ignore[return-value]
        if self._cache is not None:
            cached = self._cache.get(self._app_key)
            if isinstance(cached, Mapping):
                token, expiry = (
                    cached.get("token"),
                    _expiry(cached.get("expires_at")) if cached.get("expires_at") else None,
                )
                if isinstance(token, str) and token and expiry and self._clock() < expiry - TOKEN_MARGIN:
                    self._token, self._expires_at = token, expiry
                    return token
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
        if self._cache is not None:
            self._cache[self._app_key] = state
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
        if status == 401 or (status == 200 and body.get("rsp_cd") == "IGW40043"):
            # HTTP 401 and this business code both mean an invalid access token.
            self._discard_token()
            if not self._app_secret:
                raise ConfigurationError("NH token was rejected; supply app_secret to renew")
            self.issue_token()
            headers["authorization"] = f"Bearer {self._token}"
            self._limiter.wait("read")
            status, body, response_headers = self._post_raw(client or self._client, path, headers, data)
        if status == 401 or (status == 200 and body.get("rsp_cd") == "IGW40043"):
            self._discard_token()
            raise ConfigurationError("NH token was rejected")
        if status == 429:
            raise TransportError("NH read rate limit")
        if status != 200:
            raise TransportError(f"NH read HTTP {status}")
        code = str(body.get("rsp_cd", ""))
        nested = body.get("message")
        if nested is not None and not isinstance(nested, Mapping):
            raise InvalidResponseError("NH response message is malformed")
        message = body.get("rsp_msg") or (nested.get("usr_msg") if nested is not None else None)
        if message is not None and not isinstance(message, str):
            raise InvalidResponseError("NH response message is malformed")
        # Match an affirmative completion at the end, not the word inside
        # "완료되지 않았습니다". Known successful codes may omit a message.
        completed = bool(message and re.search(r"(?:^|\s)완료(?:되었습니다|됐습니다|했습니다|됨)?[.!\s]*$", message))
        if code not in NH_SUCCESS_CODES | NH_CONTINUATION_CODES and not completed:
            raise BrokerRejectedError("NH read rejected")
        if code in NH_SUCCESS_CODES and message and "완료" in message and not completed:
            raise BrokerRejectedError("NH read rejected")
        # Documented blocks are optional in the wire schema, but a money/price read
        # without the expected block cannot be interpreted as zero or success.
        if expected is not None and (expected not in body or body[expected] is None):
            raise InvalidResponseError(f"NH read missing {expected}")
        return body, response_headers

    def _read(
        self, path: str, payload: Mapping[str, Any], expected: str, *, client: Any | None = None
    ) -> Mapping[str, Any]:
        body, headers = self._read_response(path, payload, expected, client=client)
        page_headers = {str(key).lower(): value for key, value in headers.items()}
        if page_headers.get("cts_flag") not in (None, "N") or str(body.get("rsp_cd", "")) in NH_CONTINUATION_CODES:
            raise InvalidResponseError("NH single-result read unexpectedly needs continuation")
        return body

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            supports_limit_order=False,
            supports_market_order=False,
            supports_virtual_account=True,
            virtual=self.virtual,
            venues=(self.venue,),
            markets=("KR",),
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

        def read() -> Quote:
            row = self._read("/krstock/quote/v1/currentPrice", {"market_cd": self.venue, "iem_cd": code}, "Output_0")[
                "Output_0"
            ]
            if not isinstance(row, Mapping):
                raise InvalidResponseError("NH quote block is invalid")
            if row.get("iem_cd") is not None and row["iem_cd"] != code:
                raise InvalidResponseError("NH quote symbol does not match request")
            bid = decimal(row["bidp"]) if row.get("bidp") not in (None, "", 0, "0") else None
            ask = decimal(row["askp"]) if row.get("askp") not in (None, "", 0, "0") else None
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None
            prev = decimal(row["stck_prdy_clpr"]) if row.get("stck_prdy_clpr") not in (None, "", 0, "0") else None
            return Quote(symbol=code, last=_positive(row["stck_prpr"], "last price"), bid=bid, ask=ask, prev_close=prev)

        return self._call(read)

    def _balance(self) -> Mapping[str, Any]:
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
            flag = page_headers.get("cts_flag")
            next_cts = page_headers.get("cts")
            if flag not in (None, "Y", "N"):
                raise InvalidResponseError("NH balance continuation flag is invalid")
            code = str(body.get("rsp_cd", ""))
            if flag == "N" and code in NH_CONTINUATION_CODES:
                raise InvalidResponseError("NH balance continuation signals conflict")
            more = flag == "Y" or (flag is None and code in NH_CONTINUATION_CODES)
            if not more:
                if not isinstance(body.get("Output_0"), Mapping):
                    raise InvalidResponseError("NH balance final summary is missing")
                return {**body, "Output_1": rows}
            if not isinstance(next_cts, str) or not next_cts or next_cts in seen:
                raise InvalidResponseError("NH balance continuation key is missing or repeated")
            seen.add(next_cts)
            cts = next_cts
        raise InvalidResponseError("NH balance exceeded the page limit")

    def get_balance(self) -> Balance:
        def read() -> Balance:
            row = self._balance()["Output_0"]
            if not isinstance(row, Mapping):
                raise InvalidResponseError("NH balance block is invalid")
            # Never count margin or unsettled assets as spendable cash.
            cash = min(decimal(row["dca"]), decimal(row["orr_pbl_amt"]))
            return Balance(cash_available=cash, position_value=decimal(row["tot_eal_amt"]))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        def read() -> list[Position]:
            body = self._balance()
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
        row = self._read(
            "/krstock/inquiry/v1/sellableQuantity",
            {
                "act_no": self._account,
                "iem_cd": code,
                "cfd_lon_cd": "00",
            },
            "Output_0",
        )["Output_0"]
        if not isinstance(row, Mapping) or row.get("iem_cd") != code:
            raise InvalidResponseError("NH sellable quantity does not match symbol")
        return whole(row["sll_pbl_qty"])

    def available_qty(self, symbol: str) -> int:
        code = self._symbol(symbol, "KR")
        return self._call(lambda: self._sellable_quantity(code))

    def buyable_quantity(self, symbol: str, *, price: Decimal | None = None) -> int:
        """Cash buyable quantity at the caller's ordinary limit price."""
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
