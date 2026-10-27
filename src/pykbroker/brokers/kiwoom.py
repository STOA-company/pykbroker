"""Kiwoom KR/US broker over a stored token, with explicit au10001 issuance: reads, limit orders and single cancels.

Spec: the official portal pages of 2026-09-26 12:44 KST (openapi.kiwoom.com/guide/apiGuideContents/...), as
extracted in q-gateway plans/kiwoom_rest_spec.md. US source: official Kiwoom-Securities/Kiwoom-REST-API
kiwoom/_data/kiwoom_api_spec.json at 953e5dbff123f437ab4d11a78a95191a685eb51f (read 2026-10-04).
What those pages do not say is refused, never guessed.
"""

import json
import re
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    BrokerError,
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    TransportError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.models.order import OrderType, Side
from pykbroker.rules.kr import KST, equity_rules
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal, whole

# Hosts are copied from trader quantus_backend/traders/kiwoom_order.py and match the spec. The mock host is
# spelled here once so that `virtual` alone picks it.
KIWOOM_API_BASE_URL = "https://api.kiwoom.com"
KIWOOM_MOCK_BASE_URL = "https://mockapi.kiwoom.com"
# Spec: real 5 order and 5 read calls/s, mock «TR 1개당 1초 1회», but whether a bucket is per TR or per account is
# unstated. Every TR is at most 1/s and KR read TRs sum to 5 (3 x 1 + 4 x 0.5).
KIWOOM_TR_RATE_LIMITS: dict[str, float] = {
    "ka10001": 1, "ka10004": 1, "kt00001": 0.5, "kt00018": 1, "ka10075": 0.5, "kt00007": 0.5,
    "kt00011": 0.5, "kt10000": 1, "kt10001": 1, "kt10003": 1,
    "usa20100": 0.4, "usa20101": 0.4, "ust21050": 0.4, "ust21070": 0.4,
    "ust21110": 0.4, "ust21150": 0.4, "ust31490": 0.4,
    "ust20000": 1, "ust20001": 1, "ust20003": 1,
}  # fmt: skip
# US peak (09:00-10:00 KST): all seven read TRs total 2.8/s, three write TRs total 3/s.
# This also stays below mock one/TR/s; the KR and US budgets are documented separately.
# A stored token is the only credential the broker holds; the secret key is used once by `issue_token` and dropped.
CLIENT_OPTION_KEYS = {"token", "token_type", "expires_dt", "app_key"}
# kt00001 qry_tp is 3 (estimated) or 2 (ordinary). The official sample asks for 3, but neither is described and an
# estimate may count money that is not settled; the ordinary query is the one that cannot overstate what is spendable.
KIWOOM_CASH_QUERY_TYPE = "2"
# kt00018: dmst_stex_tp is KRX|NXT (this adapter declares KRX only); qry_tp is 1 (합산) or 2 (개별), rows unstated.
KIWOOM_POSITION_EXCHANGES = frozenset({"KRX"})
KIWOOM_POSITION_QUERY_TYPES = frozenset({"1", "2"})
# Page size and page count are unstated: a list that does not end within this many pages is refused, not cut short.
KIWOOM_MAX_PAGES = 20
# expires_dt is 14 digits (YYYYMMDDHHMMSS) in the sample; its zone and the token lifetime are not stated. It is read as
# KST, the earlier of the plausible readings, and a margin is kept: expiry is judged early, never late.
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)
_EXPIRES_DT = re.compile(r"\d{14}")
# Spec: ord_no is «7자리» (the kt10000 sample shows 5), and 0000000 is the «no original order» filler, never an order.
_ORDER_NO = re.compile(r"[0-9]{1,7}")
_US_ORDER_NO = re.compile(r"[0-9]{1,9}")
_US_VENUES = {"NASDAQ": "ND", "NYSE": "NY", "AMEX": "NA"}
_UNEXPECTED_ERROR_CODE = 1999
# trde_tp 0 (보통) is the only order type sent; ka10075/kt00007 spell types in Korean (보통 / 지정가 / 시장가), and any
# other name stays unknown.
KIWOOM_LIMIT_TRDE_TP = "0"
_TYPE_NAMES: dict[str, OrderType] = {"보통": "limit", "지정가": "limit", "시장가": "market"}
_SIDE_NAMES: dict[str, Side] = {"매수": "buy", "매도": "sell"}
_JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


class _KiwoomRestClient:
    """Copied from trader kiwoom_order.py: a narrow JSON POST boundary with no extra dependency."""

    def __init__(self, base_url: str = KIWOOM_API_BASE_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    def post(self, path: str, *, headers: dict, json: dict) -> tuple[dict, dict[str, str]]:
        """The body and the lower-cased response headers: `cont-yn` and `next-key` are the only truncation signal."""
        request = urllib.request.Request(f"{self.base_url}{path}", data=_dumps(json), headers=headers, method="POST")
        with urlopen(request, timeout=self._timeout) as response:
            return _loads(response.read()), {key.lower(): value for key, value in response.headers.items()}


def _dumps(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _loads(value: bytes) -> dict:
    decoded = json.loads(value.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Kiwoom response must be a JSON object")
    return decoded


def _parse_expires_dt(value: Any) -> datetime:
    if not isinstance(value, str) or not _EXPIRES_DT.fullmatch(value):
        raise ValueError("expires_dt must be 14 digits")
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _unpack(reply: Any, *, write: bool = False) -> tuple[Mapping[str, Any], Mapping[str, str]]:
    if not (isinstance(reply, tuple) and len(reply) == 2 and all(isinstance(part, Mapping) for part in reply)):
        raise InvalidResponseError("Kiwoom client must return a JSON object and its headers")
    body, headers = reply
    # Spec: success carries an integer return_code 0. The spec does not say whether failures arrive as HTTP status or
    # as a body code, so an HTTP error surfaces as a transport failure and any body code but 0 is a refusal.
    if "return_code" not in body:
        raise InvalidResponseError("Kiwoom response has no return_code")
    # A write is refused only by an integer code: a code that is not one is «unknown», never «safe to resubmit».
    if write and type(body["return_code"]) is not int:
        raise InvalidResponseError("Kiwoom write response has no integer return_code; reconcile before retrying")
    # 1999 is the error table's «예기치 못한 에러» (unexpected error): a backend failure that may arrive after the
    # order was taken, so it is not an explicit refusal. On a write it is unknown — reconcile before resubmitting.
    if write and body["return_code"] == _UNEXPECTED_ERROR_CODE:
        raise InvalidResponseError("Kiwoom reported an unexpected error (1999); reconcile before retrying")
    if type(body["return_code"]) is not int or body["return_code"] != 0:
        error = BrokerRejectedError("Kiwoom rejected the request")
        # The integer vendor code only (never the message): callers classify e.g. 8001 앱키/시크릿 오류 by it.
        error.return_code = body["return_code"] if type(body["return_code"]) is int else None
        raise error
    return body, {str(key).lower(): value for key, value in headers.items()}


def _order_number(value: Any, *, market: str = "KR") -> str:
    pattern = _US_ORDER_NO if market == "US" else _ORDER_NO
    if not (isinstance(value, str) and pattern.fullmatch(value) and int(value)):
        raise InvalidResponseError("Kiwoom order number is not the documented form; reconcile before retrying")
    return value


def _price(value: Any) -> Decimal:
    """Price strings carry direction: official ka10095 has lst_pric=-85200, base_pric=121700."""
    if isinstance(value, str):
        try:
            value = Decimal(value).copy_abs()
        except InvalidOperation:
            raise InvalidResponseError("Invalid price field") from None
    return decimal(value)


def _cash_amount(value: Any) -> Decimal:
    """KR cash deficits remain zero spendable cash; nonfinite/malformed fields are refused."""
    if value is None or isinstance(value, bool):
        raise InvalidResponseError("Missing or invalid cash field")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise InvalidResponseError("Invalid cash field") from None
    if not amount.is_finite():
        raise InvalidResponseError("Nonfinite cash field")
    return max(amount, Decimal(0))


def _average_fill(row: Mapping[str, Any], filled: int) -> Decimal | None:
    amount = row.get("cntr_tot_amt")
    if not filled or amount is None or (isinstance(amount, str) and not amount.strip()):
        return None
    total = decimal(amount)
    return total / filled if total > 0 else None


def _optional_price(value: Any) -> Decimal | None:
    """Absent, empty or zero is «the venue did not state it» (the after-hours ka10004 sample is all "0")."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _price(value) or None


class Kiwoom(BaseBroker):
    broker_code = "kiwoom"
    supports_virtual = True
    us_venues = tuple(_US_VENUES)

    def __init__(
        self,
        *,
        account: str,
        client_options: Mapping[str, Any],
        client_factory: Callable[..., Any] | None = None,
        codes: Mapping[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        virtual: bool = False,
        venue: str | None = None,
        is_integrated: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        if market == "US" and venue not in _US_VENUES:
            raise ConfigurationError("Kiwoom US requires NASDAQ, NYSE or AMEX venue")
        if market == "KR" and venue not in (None, "KRX"):
            raise NotSupportedError("Kiwoom KR supports KRX only")
        if not isinstance(is_integrated, bool):
            raise ConfigurationError("is_integrated must be an explicit boolean")
        self.venue = venue
        self._us_native_halts: dict[tuple[str | None, str], bool | None] = {}
        self.is_integrated = is_integrated
        options = dict(client_options)
        if not set(options) <= CLIENT_OPTION_KEYS:
            raise ConfigurationError("client_options accepts only token, token_type, expires_dt and app_key")
        token, app_key = options.get("token"), options.get("app_key")
        if not (isinstance(token, str) and token.strip() and isinstance(app_key, str) and app_key.strip()):
            raise ConfigurationError("Supply a stored Kiwoom token and app_key; Kiwoom.issue_token obtains one")
        # The header spells the type «Bearer»; the issuance sample spells it lowercase. Any other type is unverified.
        if str(options.get("token_type", "Bearer")).lower() != "bearer":
            raise ConfigurationError("Only Bearer tokens are verified")
        self._authorization = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        # Without expires_dt the server is the arbiter (it refuses a stale token); with it, a stale token is not sent.
        try:
            self._expires_at = _parse_expires_dt(options["expires_dt"]) if "expires_dt" in options else None
        except ValueError:
            raise ConfigurationError("expires_dt must be the 14-digit value returned at issuance") from None
        self._clock = clock
        self._codes = dict(codes or {})
        self._limiter = rate_limiter or credential_rate_limiter("kiwoom", app_key, KIWOOM_TR_RATE_LIMITS)
        # The host follows `virtual` alone; the factory cannot choose it.
        host = KIWOOM_MOCK_BASE_URL if virtual else KIWOOM_API_BASE_URL
        self._client = (client_factory or _KiwoomRestClient)(base_url=host)

    @classmethod
    def issue_token(
        cls,
        *,
        app_key: str,
        secret_key: str,
        virtual: bool = False,
        client_factory: Callable[..., Any] | None = None,
    ) -> dict[str, str]:
        """au10001. Returns token, token_type and expires_dt exactly as issued, for the caller to store.

        Only an explicit call issues: reissuing may invalidate a token another process holds (unstated), so no
        request ever reissues on its own. The secret key is neither kept nor echoed. The spec's au10001 page has no
        header table, so no `api-id` is sent.
        """
        if not isinstance(virtual, bool):
            raise ConfigurationError("virtual must be a boolean; the account mode is never inferred")
        if not all(isinstance(value, str) and value.strip() for value in (app_key, secret_key)):
            raise ConfigurationError("Supply app_key and secret_key")
        client = (client_factory or _KiwoomRestClient)(
            base_url=KIWOOM_MOCK_BASE_URL if virtual else KIWOOM_API_BASE_URL
        )
        body = {"grant_type": "client_credentials", "appkey": app_key, "secretkey": secret_key}
        try:
            response, _ = _unpack(client.post("/oauth2/token", headers=dict(_JSON_HEADERS), json=body))
            issued = {name: response[name] for name in ("token", "token_type", "expires_dt")}
            if not all(isinstance(value, str) and value for value in issued.values()):
                raise ValueError("token fields must be non-empty strings")
            _parse_expires_dt(issued["expires_dt"])
            return issued
        except BrokerError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise InvalidResponseError("Vendor token response is invalid") from None
        except Exception:
            raise TransportError("Token issuance failed; an earlier token may or may not still be valid") from None

    def _post(
        self, path: str, tr: str, body: dict[str, Any], continuation: str | None = None, *, write: bool = False
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        if self._expires_at is not None and self._clock() >= self._expires_at - TOKEN_EXPIRY_MARGIN:
            raise ConfigurationError("The stored Kiwoom token is expired or about to expire; issue a new one")
        headers = {**_JSON_HEADERS, "authorization": self._authorization, "api-id": tr}
        if continuation is not None:
            headers.update({"cont-yn": "Y", "next-key": continuation})
        self._limiter.wait(tr)
        return _unpack(self._client.post(path, headers=headers, json=body), write=write)

    def _rows(self, path: str, tr: str, body: dict[str, Any], key: str) -> list[Mapping[str, Any]]:
        """Every page of a list TR. Spec: the response headers `cont-yn`=Y and `next-key` are set on the next request
        as they came. A list whose end cannot be shown is refused: a truncated holdings list reads as a smaller one."""
        rows: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        continuation: str | None = None
        for _ in range(KIWOOM_MAX_PAGES):
            response, headers = self._post(path, tr, body, continuation)
            page = response[key]
            if not isinstance(page, list):
                raise InvalidResponseError("Kiwoom list field must be a list")
            rows.extend(page)
            if headers.get("cont-yn") == "N":
                return rows
            continuation = headers.get("next-key")
            if headers.get("cont-yn") != "Y" or not continuation or continuation in seen:
                raise InvalidResponseError("Kiwoom continuation headers do not show where the list ends")
            seen.add(continuation)
        raise InvalidResponseError("Kiwoom list exceeded the page limit; refusing a truncated result")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            supports_limit_order=True,
            supports_single_order_cancel=True,
            supports_order_history=True,
            # ka10075 is a snapshot, but the spec does not say how current it is: not declared real-time.
            supports_virtual_account=True,
            virtual=self.virtual,
            requires_venue=self.market == "US",
            venues=self.us_venues if self.market == "US" else ("KRX",),
            markets=(self.market,),
        )

    def _basic(self, code: str) -> Mapping[str, Any]:
        return self._post("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})[0]

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        code = self._symbol(symbol, market)

        if self.market == "US":
            return self._call(lambda: self._us_quote(code))

        def read() -> Quote:
            last = _price(self._basic(code)["cur_prc"])
            book = self._post("/api/dostk/mrkcond", "ka10004", {"stk_cd": code})[0]
            # 매수 최우선호가 is the bid, 매도 최우선호가 the ask. A crossed pair (a call auction, or a swapped
            # mapping) is not a spread anyone can trade on, so it is reported as unknown.
            bid, ask = _optional_price(book.get("buy_fpr_bid")), _optional_price(book.get("sel_fpr_bid"))
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None
            return Quote(symbol=code, last=last, bid=bid, ask=ask)

        return self._call(read)

    def get_rules(
        self, symbol: str, *, now: datetime | None = None, trading_day: Callable[[date], bool] | None = None
    ) -> Rules:
        if self.market == "US":
            code = self._symbol(symbol, "US")
            key = (self.venue, code)
            if key not in self._us_native_halts:
                self.get_quote(code, market="US")
            rules = super().get_rules(code, now=now, trading_day=trading_day)
            # A caller halt can veto native normal; caller False cannot fill a native silence.
            halted = True if rules.halted is True else self._us_native_halts[key]
            return rules.model_copy(update={"halted": halted})
        code = self._symbol(symbol, "KR")

        def read() -> tuple[Decimal, Decimal | None, Decimal | None]:
            row = self._basic(code)
            last = _price(row["cur_prc"])
            upper, lower = _optional_price(row.get("upl_pric")), _optional_price(row.get("lst_pric"))
            base = _optional_price(row.get("base_pric"))
            if upper is None or lower is None:
                return last, None, None  # half a band is not a band; nothing is derived from 기준가 either
            # The official ka10001 sample is field-misaligned (a date in upl_pric, a percent in lst_pric), so a band
            # that does not contain the last and reference price is a mapping fault, not a band.
            if not lower <= last <= upper or (base is not None and not lower <= base <= upper):
                raise InvalidResponseError("Kiwoom daily band does not contain the reference and last prices")
            return last, upper, lower

        last, upper, lower = self._call(read)
        return equity_rules(
            code,
            last,
            halted=self._halt_from_provider(code),
            now=now,
            trading_day=trading_day,
            limit_up=upper,
            limit_down=lower,
        )

    def _cash(self) -> Decimal:
        if self.market == "US":
            rows = self._rows("/api/us/acnt", "ust21110", {}, "result_list")
            usd = [row for row in rows if row.get("crnc_code") == "USD"]
            if len(usd) != 1:
                raise InvalidResponseError("Kiwoom cash must contain exactly one USD row")
            return min(decimal(usd[0]["fc_entra"]), decimal(usd[0]["fc_pymn_alowa"]))
        row = self._post("/api/dostk/acnt", "kt00001", {"qry_tp": KIWOOM_CASH_QUERY_TYPE})[0]
        # `ord_alow_amt` (주문가능금액) is not documented as cash-only and the official sample shows it above the
        # deposit `entr` (85,341 vs 17,534), so it is capped by the deposit. `prsm_dpst_aset_amt` is never used.
        # 100% stock allowance and D+2 deposit include today's unsettled buys (official example: 12,550).
        return min(_cash_amount(row[key]) for key in ("ord_alow_amt", "entr", "100stk_ord_alow_amt", "d2_entra"))

    def available_cash(self) -> Decimal:
        return self._call(self._cash)

    def get_balance(self) -> Balance:
        def read() -> Balance:
            value = sum((position.market_value for position in self._positions()), Decimal(0))
            return Balance(
                currency="USD" if self.market == "US" else "KRW", cash_available=self._cash(), position_value=value
            )

        return self._call(read)

    def _positions(self) -> list[Position]:
        if self.market == "US":
            return self._us_positions()
        exchange, query = self._codes.get("exchange", "KRX"), self._codes.get("query_type")
        if exchange not in KIWOOM_POSITION_EXCHANGES or query not in KIWOOM_POSITION_QUERY_TYPES:
            raise ConfigurationError("Supply codes exchange (KRX) and query_type (1 or 2, see the kt00018 spec)")
        rows = self._rows(
            "/api/dostk/acnt", "kt00018", {"dmst_stex_tp": exchange, "qry_tp": query}, "acnt_evlt_remn_indv_tot"
        )
        return [
            Position(
                symbol=self._symbol(row["stk_cd"], "KR"),
                name=row["stk_nm"],
                quantity=whole(row["rmnd_qty"]),
                sellable=whole(row["trde_able_qty"]),
                average_price=_price(row["pur_pric"]),
                last=_price(row["cur_prc"]),
                market_value=decimal(row["evlt_amt"]),
            )
            for row in rows
        ]

    def get_positions(self) -> list[Position]:
        return self._call(self._positions)

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
        if isinstance(request, OrderRequest) and request.order_type != "limit":
            raise NotSupportedError("Kiwoom implements limit orders only")
        pattern = _US_ORDER_NO if self.market == "US" else _ORDER_NO
        if isinstance(request, CancelRequest) and not (pattern.fullmatch(request.order_id) and int(request.order_id)):
            # all zeros is the «no original order» filler, never a real order number
            raise ConfigurationError("Kiwoom order number format is invalid for this market")

    def _write(self, path: str, tr: str, body: dict[str, Any]) -> Mapping[str, Any]:
        """One POST, never repeated: this client does not retry and no caller loop may. A refusal is a return_code
        integer other than 0; a failed send or an unreadable answer is unknown, so `_call` reports it as such."""
        response = self._post(path, tr, body, write=True)[0]
        _order_number(response.get("ord_no"), market=self.market)
        return response

    def _place_order(self, request: OrderRequest) -> Order:
        code = self._symbol(request.symbol, request.market)
        if self.market == "US":
            return self._us_place(request, code)
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": code,
            "ord_qty": str(request.quantity),
            "ord_uv": str(int(request.price)),
            "trde_tp": KIWOOM_LIMIT_TRDE_TP,
        }
        response = self._write("/api/dostk/ordr", "kt10000" if request.side == "buy" else "kt10001", body)
        if response.get("dmst_stex_tp", "KRX") != "KRX":
            raise InvalidResponseError("Kiwoom order went to another exchange; reconcile before retrying")
        # 접수 is not 체결: the acknowledgement says nothing about fills.
        return Order(
            id=response["ord_no"],
            symbol=code,
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self.virtual,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        code = self._symbol(request.symbol, request.market)
        if self.market == "US":
            return self._us_cancel(request, code)
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": request.order_id,
            "stk_cd": code,
            "cncl_qty": str(request.quantity),
        }
        response = self._write("/api/dostk/ordr", "kt10003", body)
        # ord_no is the cancel's own number; base_orig_ord_no is not compared (its meaning is unstated). A different
        # cancelled quantity than asked is not an outcome this adapter can name.
        if whole(response["cncl_qty"]) != request.quantity:
            raise InvalidResponseError("Kiwoom cancelled another quantity; reconcile before retrying")
        # A cancel acknowledgement does not establish the final state of the order.
        return Order(id=request.order_id, symbol=code, status="accepted", virtual=self.virtual)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if self.market == "US":
            return self._call(lambda: self._us_orders(symbol))
        code = self._symbol(symbol, self.market) if symbol is not None else None
        body = {"all_stk_tp": "0" if code is None else "1", "trde_tp": "0", "stex_tp": "1"}
        if code is not None:
            body["stk_cd"] = code

        def read() -> list[Order]:
            orders = []
            for row in self._rows("/api/dostk/acnt", "ka10075", body, "oso"):
                listed = self._symbol(row["stk_cd"], "KR")
                if code is not None and listed != code:
                    continue
                filled, remaining = whole(row["cntr_qty"]), whole(row["oso_qty"])
                orders.append(
                    Order(
                        id=_order_number(row["ord_no"]),
                        symbol=listed,
                        side=_SIDE_NAMES.get(str(row["io_tp_nm"]).lstrip("+-")),  # +/- only colours the label
                        order_type=_TYPE_NAMES.get(row["trde_tp"]),
                        quantity=whole(row["ord_qty"]),
                        price=_optional_price(row["ord_pric"]),
                        filled=filled,
                        fill_price=_average_fill(row, filled),
                        remaining=remaining,
                        status=("partial" if filled else "open") if remaining else "unknown",
                        virtual=self.virtual,
                    )
                )
            return orders

        return self._call(read)

    def status(self, order: Order) -> Order:
        """kt00007 for today (KST) by order number. An order the ledger does not show exactly once, or whose numbers
        do not add up, is unknown. Only confirmed, linked cancel quantities establish a cancellation."""
        code = self._symbol(order.symbol, order.market)
        if self.market == "US":
            return self._call(lambda: self._us_status(order, code))
        today = self._clock().astimezone(KST).strftime("%Y%m%d")
        body = {
            "ord_dt": today,
            "qry_tp": "1",
            "stk_bond_tp": "1",
            "sell_tp": "0",
            "stk_cd": code,
            "dmst_stex_tp": "KRX",
        }

        def read() -> Order:
            rows = self._rows("/api/dostk/acnt", "kt00007", body, "acnt_ord_cntr_prps_dtl")
            mine = [row for row in rows if row.get("ord_no") == order.id]
            if len(mine) != 1 or self._symbol(mine[0]["stk_cd"], "KR") != code:
                return order.model_copy(update={"status": "unknown"})
            quantity, filled, remaining = (whole(mine[0][name]) for name in ("ord_qty", "cntr_qty", "ord_remnq"))
            changes = [
                row for row in rows if row.get("ori_ord") == order.id and str(row.get("mdfy_cncl") or "").strip()
            ]
            status = "unknown"
            if quantity > 0 and filled == quantity and not remaining:
                # A late amendment/cancel row cannot undo the original order's complete execution.
                status = "filled"
            else:
                # kt00007 cnfm_qty is confirmed quantity, not the requested ord_qty.
                cancel_ids = [row.get("ord_no") for row in changes]
                # Merge of #49 (venue/symbol/own-id/unique-id checks) and #63 (ASSUMED: kt00007 leaves mdfy_cncl blank; «취소»/«취소확인» unspecified, other values stay unknown).
                confirmed = all(
                    str(row.get("mdfy_cncl")).strip() in {"취소", "취소확인"}
                    and self._symbol(row["stk_cd"], "KR") == code
                    and row.get("dmst_stex_tp") == "KRX"
                    and row.get("ord_no") != order.id
                    for row in changes
                ) and len(set(cancel_ids)) == len(cancel_ids)
                cancelled = 0
                if confirmed and changes:
                    # cnfm_qty is the cancelled quantity (#49); #63: absent on every row -> the rows' ord_qty;
                    # present on only some rows -> unknown. Conservation below still has to close.
                    given = [whole(row["cnfm_qty"]) for row in changes if row.get("cnfm_qty") not in (None, "")]
                    if not given:
                        cancelled = sum(whole(row["ord_qty"]) for row in changes)
                    elif len(given) == len(changes):
                        cancelled = sum(given)
                    else:
                        confirmed = False
                if confirmed and quantity > 0 and filled + cancelled + remaining == quantity:
                    if cancelled and not remaining:
                        status = "cancelled"
                    elif remaining and (not changes or cancelled > 0):
                        status = "partial" if filled else "open"
            return order.model_copy(
                update={
                    "status": status,
                    "quantity": quantity,
                    "filled": filled,
                    "remaining": remaining,
                    "fill_price": _optional_price(mine[0].get("cntr_uv")) if filled == 1 else None,
                }
            )

        return self._call(read)

    def _us_body(self, code: str) -> dict[str, str]:
        return {"stex_tp": _US_VENUES[self.venue], "stk_cd": code}

    def _us_quote(self, code: str) -> Quote:
        key = (self.venue, code)
        self._us_native_halts.pop(key, None)
        basic = self._post("/api/us/mrkcond", "usa20100", self._us_body(code))[0]
        book = self._post("/api/us/mrkcond", "usa20101", self._us_body(code))[0]
        bid, ask = _optional_price(book.get("fpr_buy_bid")), _optional_price(book.get("fpr_sel_bid"))
        if bid is not None and ask is not None and bid > ask:
            bid = ask = None
        # dt/bid_tm have no documented timezone; a receipt clock is not a source timestamp.
        quote = Quote(
            symbol=code,
            market="US",
            last=_price(basic["cur_prc"]),
            bid=bid,
            ask=ask,
            prev_close=_optional_price(basic.get("base_close_pric")),
        )
        # Official /guide/apiGuideContents/33/usa20100: trd_susp_tp "0" is normal;
        # suspended instruments are not returned. No other code meaning is documented.
        self._us_native_halts[key] = (
            False
            if type(basic.get("trd_susp_tp")) is str
            and basic["trd_susp_tp"] == "0"
            and basic.get("stk_cd") == code
            and basic.get("stex_tp") == _US_VENUES[self.venue]
            and quote.last > 0
            else None
        )
        return quote

    def _us_positions(self) -> list[Position]:
        rows = self._rows("/api/us/acnt", "ust21070", self._us_body(""), "result_list")
        positions = []
        for row in rows:
            if row.get("crnc_code") != "USD":
                raise InvalidResponseError("Kiwoom US holding currency is not USD")
            positions.append(
                Position(
                    symbol=self._symbol(row["stk_cd"], "US"),
                    name=row["frgn_stk_nm"],
                    quantity=whole(row["poss_qty"]),
                    sellable=whole(row["sell_alowq"]),
                    average_price=_price(row["frgn_stk_book_uv"]),
                    last=_price(row["now_pric"]),
                    market_value=decimal(row["evlt_amt"]),
                    market="US",
                )
            )
        return positions

    def orderable_quantity(self, symbol: str, price: Decimal) -> int:
        """kt00011 (KR) / ust31490 (US) no-debt quantity. No cash/FX/asset-value conversion.

        The existing account's explicit is_integrated selects the broker's KRW-order column,
        only when the broker confirms that the account has applied for KRW orders.
        This read neither enrolls the account nor changes order submission safety.
        """
        code = self._symbol(symbol, self.market)
        price = decimal(price)
        if not price:
            raise ConfigurationError("Supply a positive order price")

        def read() -> int:
            if self.market == "KR":
                if price != price.to_integral_value():
                    raise ConfigurationError("KR order price must be whole won")
                row = self._post("/api/dostk/acnt", "kt00011", {"stk_cd": code, "uv": format(price, "f")})[0]
                return whole(row["min_ord_alowq"])
            row = self._post("/api/us/ordr", "ust31490", {**self._us_body(code), "uv": format(price, "f")})[0]
            if row.get("crnc_code") != "USD":
                raise InvalidResponseError("Kiwoom orderability currency is not USD")
            key = "min_ord_alowq"
            if self.is_integrated:
                if row.get("krw_ord_rqst_yn") != "Y":
                    raise NotSupportedError("The broker has not enabled KRW orders for this account")
                key = "min_krw_ord_alowq"
            return whole(row[key])

        return self._call(read)

    def _us_place(self, request: OrderRequest, code: str) -> Order:
        response = self._write(
            "/api/us/ordr",
            "ust20000" if request.side == "buy" else "ust20001",
            {
                **self._us_body(code),
                "ord_qty": str(request.quantity),
                "ord_uv": format(request.price, "f"),
                "trde_tp": "00",
            },
        )
        return Order(
            id=response["ord_no"],
            symbol=code,
            market="US",
            side=request.side,
            order_type="limit",
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self.virtual,
        )

    def _us_cancel(self, request: CancelRequest, code: str) -> Order:
        # ust20003 has no quantity parameter and cancels the entire order. Refuse partial cancels
        # before a POST; a concurrent external amendment/fill still requires reconciliation.
        mine = [order for order in self._us_orders(code) if order.id == request.order_id]
        if len(mine) != 1 or mine[0].remaining != request.quantity or mine[0].status not in {"open", "partial"}:
            raise NotSupportedError("Kiwoom US cancel requires the full confirmed remaining quantity")
        response = self._write("/api/us/ordr", "ust20003", {**self._us_body(code), "orig_ord_no": request.order_id})
        if whole(response["cncl_ord_qty"]) != request.quantity:
            raise InvalidResponseError("Kiwoom cancelled another quantity; reconcile before retrying")
        return Order(id=request.order_id, symbol=code, market="US", status="accepted", virtual=self.virtual)

    def _us_order_row(self, row: Mapping[str, Any]) -> Order:
        quantity, filled, remaining = (whole(row[name]) for name in ("ord_qty", "cntr_qty", "ord_remnq"))
        if row.get("crnc_code") != "USD":
            raise InvalidResponseError("Kiwoom order currency is not USD")
        changed = whole(row["mdfy_qty"]) or whole(row["cncl_qty"])
        status = "unknown"
        if not changed and filled + remaining == quantity:
            if filled and not remaining:
                status = "filled"
            elif remaining:
                status = "partial" if filled else "open"
        # cntr_uv is documented as execution unit price, not weighted average. Preserve unknown.
        # A single filled share necessarily has one unit price; the broker price can be preserved then.
        return Order(
            id=_order_number(row["ord_no"], market="US"),
            symbol=self._symbol(row["stk_cd"], "US"),
            market="US",
            side={"1": "sell", "2": "buy"}.get(row.get("slby_tp")),
            order_type={"00": "limit", "03": "market", "지정가": "limit"}.get(row.get("frgn_trde_tp")),
            price=_optional_price(row.get("ord_uv")),
            quantity=quantity,
            filled=filled,
            remaining=remaining,
            status=status,
            fill_price=_optional_price(row.get("cntr_uv")) if filled == 1 else None,
            virtual=self.virtual,
        )

    def _us_orders(self, symbol: str | None) -> list[Order]:
        code = self._symbol(symbol, "US") if symbol is not None else ""
        rows = self._rows(
            "/api/us/acnt", "ust21050", {**self._us_body(code), "ord_dt": "", "slby_tp": "0"}, "result_list"
        )
        return [
            self._us_order_row(row)
            for row in rows
            if (not code or row.get("stk_cd") == code) and row.get("ord_cntr_tp") == "10"
        ]

    def _us_status(self, order: Order, code: str) -> Order:
        # Empty ord_dt is explicitly documented as today's ledger; no timezone inference.
        rows = self._rows(
            "/api/us/acnt",
            "ust21150",
            {
                **self._us_body(code),
                "ord_dt": "",
                "query_tp": "1",
                "slby_tp": "0",
                "oppo_trde_tp": "%",
                "fr_ord_no": "",
            },
            "result_list",
        )
        mine = [row for row in rows if row.get("ord_no") == order.id and row.get("stk_cd") == code]
        if len(mine) != 1:
            return order.model_copy(update={"status": "unknown"})
        return self._us_order_row(mine[0])
