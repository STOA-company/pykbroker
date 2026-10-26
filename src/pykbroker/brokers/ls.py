"""LS KR/US REST mappings with an injected boundary and default-closed writes.

Field/path source: official TR guide version 20261007070834325 (2026-10-08).
Official public live-client code establishes all-zero rsp_cd and per-TR US read
successes; COSAT00301 detail supplies a blank BrkTpCode in its Python example.
Persisted account tokens use the shared no-redirect transport/rate limiter.
Real writes default OFF, require explicit opt-in, broker account identity read,
and OrderSafety. Mock REST binding, unified margin enrollment, average fill
price and continued US balances remain unverified. Explicit token issuance uses
the documented form boundary; expiration never triggers an automatic write replay.
"""

import json
import re
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from pykbroker._http import urlopen
from pykbroker.brokers._common import BaseBroker, OrderLimit, PageRows, decimal, optional_price, whole
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    IntegratedNotSubscribedError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.experimental._rest import RestBroker
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
from pykbroker.safety.rate_limit import credential_rate_limiter

# Field contracts: tests/fixtures/ls/official, guide version 20261007070834325.
# https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
LS_READ_SUCCESS = {"COSAQ00102": {"00136"}, "COSOQ00201": {"00001"}}
LS_MAX_PAGES = 20
LS_RATES = {
    "t1102": 10,
    "CSPAQ12200": 1,
    "t0424": 2,
    "t0425": 2,
    "CSPAT00601": 10,
    "CSPAT00701": 3,
    "CSPAT00801": 3,
    "g3101": 10,
    "g3104": 10,
    "COSOQ00201": 1,
    "COSAQ00102": 1,
    "COSAT00301": 10,
    "COSOQ02701": 1,
}
LS_CODES = {
    "exchange": "K",
    "balance_type": "0",
    "prcgb": "1",
    "chegb": "2",
    "dangb": "0",
    "charge": "0",
    "order_filter": "0",
    "side_all": "0",
    "sort_order": "2",
    "buy": "2",
    "sell": "1",
    "limit": "00",
    "market": "03",
    "cash": "000",
    "condition": "0",
    "member": "KRX",
    "broker_type": "",
    # CSPAT00601/00701 OrdprcPtnCode (호가유형코드, official TR guide): one data row per order kind.
    "conditional_limit": "05",
    "best": "06",
    "priority": "07",
    "midpoint": "12",
    "pre_close": "61",     # 장개시전시간외종가
    "after_close": "81",   # 시간외종가
    "after_single": "82",  # 시간외단일가
    # OrdCndiTpCode (주문조건구분): "condition" = 0 없음 (day), 1 IOC, 2 FOK.
    "tif_ioc": "1",
    "tif_fok": "2",
}
# MbrNo (회원사번호): KRX / NXT; any other input is processed as KRX by LS, so only these two are offered.
LS_KR_VENUES = ("KRX", "NXT")
LS_KR_EXTRA_ORDER_TYPES = ("conditional_limit", "best", "priority", "midpoint")
LS_SESSION_ORDER_KEYS = {("pre", "market"): "pre_close", ("after", "market"): "after_close",
                         ("single_price", "limit"): "after_single"}
LS_US_VENUES = {"NYSE": "81", "NASDAQ": "82"}
# Holdings continuation (tr_cont/tr_cont_key headers, t0424 cts_expcode): a list that does not
# end within this many pages is refused, never cut short.
LS_MAX_PAGES = 20
# Official row identity: t0424OutBlock1 symbol + 잔고구분 + 대출일자 + 만기일자;
# COSOQ00201OutBlock4 symbol + currency + 잔고구분 + market + loan fields.
LS_KR_ROW_KEY = ("expcode", "jangb", "loandt", "lastdt")
LS_US_ROW_KEY = ("ShtnIsuNo", "IsuNo", "CrcyCode", "AstkBalTpCode", "FcurrMktCode", "LoanDt",
                 "LoanDtlClssCode", "DueDt")
LS_SPEC_VERIFIED = False  # Global veto only; True never enables live orders.
# Official REST address; mock/real account binding is still unverified.
LS_API_BASE_URL = "https://openapi.ls-sec.co.kr:8080"
# UNVERIFIED: no distinct mock host established; do not invent one or reuse live.
LS_MOCK_BASE_URL: str | None = None


class _LsRestClient:
    """One POST attempt through the shared no-redirect boundary; no replay."""

    def __init__(self):
        self.base_url = LS_API_BASE_URL
        self.response_headers = {}

    def post(self, path, *, headers, json):
        return self._post(path, headers, _json_bytes(json))

    def post_form(self, path, *, headers, form):
        return self._post(path, headers, urlencode(form).encode("ascii"))

    def _post(self, path, headers, data):
        self.response_headers = {}
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=10.0) as response:
                self.response_headers = dict(response.headers)
                return _json_object(response.read())
        except (InvalidResponseError, TransportError):
            raise
        except Exception:
            raise TransportError("LS transport failed; reconcile writes before retrying") from None


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _json_object(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        raise InvalidResponseError("LS response is not JSON") from None
    if not isinstance(result, dict):
        raise InvalidResponseError("LS response is not an object")
    return result


class LS(RestBroker):
    broker_code = "ls"
    supports_virtual = True  # Accept explicit mode; missing mock contract still fails closed.

    us_venues = tuple(LS_US_VENUES)

    def __init__(
        self,
        *,
        virtual: bool = False,
        spec_verified: bool = False,
        market: str = "KR",
        venue: str | None = None,
        is_integrated: bool = False,
        order_date: date | None = None,
        client_options: Mapping | None = None,
        allow_live_orders: bool = False,
        clock: Callable[[], datetime] | None = None,
        **kwargs,
    ) -> None:
        if not isinstance(spec_verified, bool) or not isinstance(is_integrated, bool):
            raise ConfigurationError("LS verification and margin flags must be booleans")
        if type(allow_live_orders) is not bool:
            raise ConfigurationError("allow_live_orders must be a boolean")
        self.is_integrated = is_integrated
        if market == "US" and venue not in LS_US_VENUES:
            raise ConfigurationError("LS US venue must be NYSE or NASDAQ")
        if order_date is not None and type(order_date) is not date:
            raise ConfigurationError("LS order_date must be an explicit date")
        client, codes = kwargs.pop("client", None), kwargs.pop("codes", None)
        headers, limiter = kwargs.pop("headers", None), kwargs.pop("rate_limiter", None)
        options = dict(client_options or {})
        if set(options) - {"app_key", "token", "token_type", "mac_address", "expires_at"}:
            raise ConfigurationError("Unknown LS client option")
        token = options.get("token")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ConfigurationError("LS stored token must be nonempty")
        if str(options.get("token_type", "Bearer")).lower() != "bearer":
            raise ConfigurationError("LS token_type must be Bearer")
        if client is None and token:
            client = _LsRestClient()
        if token:
            headers = {
                **dict(headers or {}),
                "authorization": "Bearer " + token,
                "mac_address": options.get("mac_address", ""),
            }
            credential = options.get("app_key") or token
            if not isinstance(credential, str) or not credential:
                raise ConfigurationError("LS credential scope is invalid")
            limiter = limiter or credential_rate_limiter("ls", credential, LS_RATES)
        if allow_live_orders and (not token or virtual):
            raise ConfigurationError("LS live opt-in requires a persisted real-account token")
        self._allow_live_orders = allow_live_orders
        self._clock = clock or (lambda: datetime.now(UTC))
        self._expires_at = None
        if "expires_at" in options:
            try:
                self._expires_at = datetime.fromisoformat(options["expires_at"])
                if self._expires_at.utcoffset() is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise ConfigurationError("LS expires_at must be an ISO timestamp with a timezone") from None
        # RestBroker's constructor is domestic-only; reuse its request boundary
        # with BaseBroker's existing KR/US validation and explicit LS state.
        BaseBroker.__init__(self, virtual=virtual, market=market, **kwargs)
        self._client = client
        self._codes = {**LS_CODES, **dict(codes or {})}
        self._headers, self._limiter = dict(headers or {}), limiter
        self.venue, self._order_date = venue, order_date
        self._us_native_halts = {}
        if self._client is None:
            raise ConfigurationError("Supply an LS REST client with an explicit base_url")
        self._bound_virtual = self.virtual
        self._spec_verified = spec_verified
        self._verify_mode(self._client)

    @staticmethod
    def issue_token(*, app_key: str, app_secret: str, client=None, clock=None) -> dict[str, str]:
        """Explicit issuance; return caller-owned options, without retaining the secret.

        The TR table spells its TTL ``expire_in`` while the official response
        example uses ``expires_in``. Both are accepted; conflicting values fail.
        No API request is retried, and no clock-of-day lifetime is assumed.
        """
        if any(not isinstance(value, str) or not value.strip() for value in (app_key, app_secret)):
            raise ConfigurationError("LS token issuance requires app_key and app_secret")
        client = client if client is not None else _LsRestClient()
        if getattr(client, "base_url", None) != LS_API_BASE_URL:
            raise ConfigurationError("LS token client must use the official REST address")
        started = (clock or (lambda: datetime.now(UTC)))()
        if not isinstance(started, datetime) or started.utcoffset() is None:
            raise ConfigurationError("LS token clock must include a timezone")
        try:
            result = client.post_form(
                "/oauth2/token", headers={"content-type": "application/x-www-form-urlencoded"},
                form={"grant_type": "client_credentials", "appkey": app_key,
                      "appsecretkey": app_secret, "scope": "oob"},
            )
        except (InvalidResponseError, TransportError):
            raise
        except Exception:
            raise TransportError("LS token issuance failed; no automatic retry") from None
        try:
            token, kind = result["access_token"], result["token_type"]
            values = [result[name] for name in ("expire_in", "expires_in") if name in result]
            if (not isinstance(token, str) or not token.strip() or len(token) > 1000
                    or "\r" in token or "\n" in token or kind != "Bearer" or not values
                    or any(type(value) not in (str, int) or not re.fullmatch(r"[0-9]{1,10}", str(value))
                           for value in values)):
                raise ValueError
            ttl = int(values[0])
            if ttl <= 0 or any(int(value) != ttl for value in values):
                raise ValueError
            return {"token": token, "token_type": kind,
                    "expires_at": (started + timedelta(seconds=ttl)).isoformat()}
        except (KeyError, TypeError, ValueError, OverflowError):
            raise InvalidResponseError("LS token response is invalid") from None

    def _require_current_token(self):
        if self._expires_at is not None:
            now = self._clock()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ConfigurationError("LS token clock must include a timezone")
            if now >= self._expires_at:
                raise ConfigurationError("LS stored token is expired; explicitly issue and supply a fresh token")

    def _verify_mode(self, client) -> None:
        expected = LS_MOCK_BASE_URL if self._bound_virtual else LS_API_BASE_URL
        if not isinstance(expected, str) or not expected or expected == LS_API_BASE_URL and self._bound_virtual:
            raise ConfigurationError("LS mock address/account-mode contract is UNVERIFIED")
        host = getattr(client, "base_url", None)
        if self.virtual is not self._bound_virtual or not isinstance(host, str) or host.rstrip("/") != expected:
            raise ConfigurationError("LS client host must match the explicit account mode")

    def _mock_verified(self) -> bool:
        return LS_SPEC_VERIFIED is True and self._spec_verified is True and self._bound_virtual is True

    def _writes_verified(self):
        return self._mock_verified() or (self._bound_virtual is False and self._allow_live_orders is True)

    def capabilities(self) -> Capabilities:
        verified = self._writes_verified()
        return Capabilities(
            virtual=self._bound_virtual,
            supports_spot=verified,
            supports_limit_order=verified,
            supports_market_order=verified and self.market == "KR",
            supports_single_order_cancel=verified,
            supports_amend=verified and self.market == "KR",
            supports_nxt=verified and self.market == "KR",
            order_kinds=("limit", "market") + LS_KR_EXTRA_ORDER_TYPES
            if verified and self.market == "KR" else (("limit",) if verified else ()),
            sessions=("regular", "pre", "after", "single_price")
            if verified and self.market == "KR" else ("regular",),
            time_in_force=("day", "ioc", "fok") if verified and self.market == "KR" else ("day",),
            # UNVERIFIED: t0425 is not yet proof of real-time/history coverage.
            supports_order_history=False,
            realtime_open_orders=False,
            markets=(self.market,),
            requires_venue=self.market == "US",
            venues=LS_KR_VENUES if self.market == "KR" else self.us_venues,
        )

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
        if (isinstance(request, OrderRequest) and request.session != "regular"
                and (request.session, request.order_type) not in LS_SESSION_ORDER_KEYS):
            raise NotSupportedError("This off-session order kind is not supported")
        if isinstance(request, OrderRequest) and request.order_type == "close":
            raise NotSupportedError("LS close orders have not been verified")
        if isinstance(request, CancelRequest):
            self._order_id(request.order_id, request=True)
        if request.market == "US":
            self._us_symbol(request.symbol)

    def place_order(self, request: OrderRequest) -> Order:
        if not self._writes_verified():
            raise SpecUnverifiedError("LS writes require the explicit real-account opt-in or verified mock")
        if request.market == "US" and type(self._order_date) is not date:
            raise ConfigurationError("LS US submission requires an explicit broker order_date for reconciliation")
        request = OrderRequest.model_validate(request.model_dump())
        self._validate_order(request)
        self._verify_mode(self._client)
        self._call(self._verify_account)
        return super().place_order(request)

    def cancel_order(self, request: CancelRequest) -> Order:
        if not self._writes_verified():
            raise SpecUnverifiedError("LS writes require the explicit real-account opt-in or verified mock")
        self._verify_mode(self._client)
        self._call(self._verify_account)
        return super().cancel_order(request)

    def amend_order(self, request, *, order_id: str | None = None) -> Order:
        """KR CSPAT00701 through the shared ``amend_order(AmendRequest)`` shape.

        Request side is caller/safety context and cannot change the original order side; the amendment
        response leaves side unknown. Acceptance does not establish that a concurrent fill/cancel completed.
        """
        if not self._writes_verified():
            raise SpecUnverifiedError("LS writes require the explicit real-account opt-in or verified mock")
        if self.market != "KR":
            raise NotSupportedError("LS amendment currently supports KR cash orders only")
        return super().amend_order(request, order_id=order_id)

    def modify_order(self, order_id: str, request: OrderRequest) -> Order:
        """Pre-unification name; a thin wrapper of ``amend_order``."""
        return self.amend_order(request, order_id=order_id)

    def _prepare_amend(self, request: AmendRequest) -> None:
        super()._prepare_amend(request)
        self._order_id(request.order_id, request=True)
        self._verify_mode(self._client)
        self._call(self._verify_account)

    def _amend_order(self, request: AmendRequest, prepared: Any = None) -> Order:
        body = {"OrgOrdNo": int(self._order_id(request.order_id, request=True)),
                "IsuNo": self._order_symbol(request.symbol),
                "OrdQty": request.quantity, "OrdprcPtnCode": self._code(request.order_type),
                "OrdCndiTpCode": self._code("condition"), "OrdPrc": int(request.price or 0)}
        response = self._response("/stock/order", "CSPAT00701", {"CSPAT00701InBlock1": body}, write=True)
        return Order(id=self._order_id(response["CSPAT00701OutBlock2"]["OrdNo"]),
                     symbol=self._symbol(request.symbol, "KR"), side=None,
                     order_type=request.order_type, quantity=request.quantity,
                     price=request.price, status="accepted", virtual=self._bound_virtual)

    def _verify_account(self):
        if self._bound_virtual:
            return  # Existing injected, explicitly verified mock contract.
        if self.market == "US":
            response = self._response(
                "/overseas-stock/accno", "COSOQ02701", {"COSOQ02701InBlock1": {"RecCnt": 1, "CrcyCode": "USD"}}
            )
            row = response["COSOQ02701OutBlock1"]
        else:
            response = self._response("/stock/accno", "CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": "0"}})
            row = response["CSPAQ12200OutBlock1"]
        actual = row.get("AcntNo") if isinstance(row, dict) else None
        if not isinstance(actual, str) or actual.replace("-", "") != self._account.replace("-", ""):
            raise ConfigurationError("LS token is not bound to the requested account")

    def _code(self, name: str) -> str:
        if name == "broker_type" and self._codes.get(name) == "":
            return ""  # Official COSAT00301 Python live-request example.
        value = super()._code(name)
        if not value.strip():
            raise ConfigurationError("Unverified vendor code; supply a reviewed codes mapping")
        return value

    def _response_symbol(self, value):
        try:
            return self._symbol(value, "KR")
        except ConfigurationError:
            raise InvalidResponseError("Invalid LS response symbol") from None

    @staticmethod
    def _price(value):
        if isinstance(value, bool):
            raise InvalidResponseError("Invalid LS price")
        return optional_price(value)

    def _request(self, path, tr, body, *, tr_cont="N", tr_cont_key=""):
        self._require_current_token()
        if self._client is None or self._limiter is None:
            raise ConfigurationError("Supply a REST client and TR rate limiter")
        self._limiter.wait(tr)
        self._require_current_token()
        headers = {**self._headers, "content-type": "application/json", "tr_cd": tr,
                   "tr_cont": tr_cont, "tr_cont_key": tr_cont_key}
        return self._client.post(path, headers=headers, json=body)

    def _last_headers(self):
        headers = getattr(self._client, "response_headers", None)
        if not isinstance(headers, Mapping):
            return None
        return {str(k).lower(): v for k, v in headers.items()}

    def _response(self, path, tr, body, *, write=False, tr_cont="N", tr_cont_key="", cont_key=None):
        self._verify_mode(self._client)
        if cont_key is not None:
            # Serving-line callers (US holdings continuation) pass the previous response key.
            tr_cont, tr_cont_key = "Y", cont_key
        result = self._request(path, tr, body, tr_cont=tr_cont, tr_cont_key=tr_cont_key)
        if not isinstance(result, dict):
            raise InvalidResponseError("Invalid LS response; reconcile writes before retrying")
        code = result.get("rsp_cd")
        if not isinstance(code, str) or not code.strip():
            raise InvalidResponseError("Missing LS response code; reconcile writes before retrying")
        # Official token-present callRestTr and isApiSuccessCode; never the demo generator.
        # Extra codes are documented in that client's TR_META for the two US reads.
        if not re.fullmatch("0+", code) and (write or code not in LS_READ_SUCCESS.get(tr, set())):
            if write:
                # The TR guide has no order success-code table. Even OrdNo > 0
                # cannot prove acceptance for an unknown code; retain the claim.
                raise InvalidResponseError("Unverified LS write result; reconcile before retrying")
            raise BrokerRejectedError("LS rejected the read request")
        if write:
            block = result.get(tr + "OutBlock2")
            self._order_id(block.get("OrdNo") if isinstance(block, dict) else None)
        return result

    @staticmethod
    def _order_id(value, *, request=False):
        error = ConfigurationError if request else InvalidResponseError
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise error("Invalid LS order number; reconcile writes before retrying")
        text = str(value)
        if not text.isascii() or not text.isdecimal() or not text.strip("0"):
            raise error("Invalid LS order number; reconcile writes before retrying")
        return text

    @staticmethod
    def _rows(response, block):
        rows = response[block]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise InvalidResponseError("Invalid LS rows")
        return rows

    def _continuation(self):
        headers = getattr(self._client, "response_headers", None)
        if not isinstance(headers, Mapping):
            raise InvalidResponseError("Missing LS continuation headers")
        headers = {str(k).lower(): v for k, v in headers.items()}
        flag, key = headers.get("tr_cont"), headers.get("tr_cont_key", "")
        if flag not in ("N", "Y") or not isinstance(key, str):
            raise InvalidResponseError("Invalid LS continuation headers")
        if flag == "N" and key.strip() or flag == "Y" and not key.strip():
            raise InvalidResponseError("Inconsistent LS continuation headers")
        return flag, key

    def _pages(self, path, tr, body, *, block=None, cursor=None):
        """Official header continuation plus domestic CTS; never expose a truncated list."""
        flag, key, seen = "N", "", set()
        for _ in range(LS_MAX_PAGES):
            response = self._response(path, tr, body, tr_cont=flag, tr_cont_key=key)
            flag, key = self._continuation()
            if cursor is not None:
                meta = response.get(block)
                value = meta.get(cursor) if isinstance(meta, dict) else None
                if not isinstance(value, str):
                    raise InvalidResponseError("Missing LS continuation cursor")
                if (flag == "Y") != bool(value.strip()):
                    raise InvalidResponseError("Inconsistent LS continuation cursor")
            if flag == "Y":
                if key in seen:
                    raise InvalidResponseError("Repeated LS continuation key")
                seen.add(key)
            yield response
            if flag == "N":
                return
            if cursor is not None:
                inblock = tr + "InBlock"
                body = {**body, inblock: {**body[inblock], cursor: value}}
        raise InvalidResponseError("LS list exceeded the page limit; refusing a truncated result")

    def _order_symbol(self, symbol):
        # Both forms are official for real stock/ETF; mock requires A+code.
        code = self._symbol(symbol, "KR")
        return "A" + code if self._bound_virtual else code

    def get_quote(self, symbol: str, *, market: str | None = None) -> Quote:
        market = market or self.market
        symbol = self._symbol(symbol, market)
        if market == "US":
            symbol = self._us_symbol(symbol)
            exchange = LS_US_VENUES[self.venue]

            def read_us():
                row = self._response(
                    "/overseas-stock/market-data",
                    "g3101",
                    {
                        "g3101InBlock": {
                            "delaygb": "R",
                            "keysymbol": exchange + symbol,
                            "exchcd": exchange,
                            "symbol": symbol,
                        },
                    },
                )["g3101OutBlock"]
                if row["symbol"] != symbol or row["currency"] != "USD" or row["exchcd"] != exchange:
                    raise InvalidResponseError("LS quote identity/currency does not match")
                suspend, sellonly = row.get("suspend"), row.get("sellonly")
                self._us_native_halts[symbol] = (
                    False if suspend == "N" and sellonly == "0" else
                    True if suspend == "Y" or sellonly in {"1", "2"} else None)
                # g3104 is an exact-symbol read; pcls is the native previous close.
                info = self._response(
                    "/overseas-stock/market-data", "g3104",
                    {"g3104InBlock": {"delaygb": "R", "keysymbol": exchange + symbol,
                                      "exchcd": exchange, "symbol": symbol}},
                )["g3104OutBlock"]
                if (info["symbol"] != symbol or info["currency"] != "USD"
                        or info["exchcd"] != exchange or info["keysymbol"] != exchange + symbol):
                    raise InvalidResponseError("LS previous close identity/currency does not match")
                return Quote(symbol=symbol, market="US", last=decimal(row["price"]),
                             prev_close=self._price(info.get("pcls")))

            return self._call(read_us)
        return self._call(
            lambda: Quote(
                symbol=symbol,
                last=decimal(
                    self._response(
                        "/stock/market-data",
                        "t1102",
                        {"t1102InBlock": {"shcode": symbol, "exchgubun": self._code("exchange")}},
                    )["t1102OutBlock"]["price"]
                ),
            )
        )

    def get_rules(self, symbol, *, now=None, trading_day=None):
        if self.market != "US":
            return super().get_rules(symbol, now=now, trading_day=trading_day)
        symbol = self._us_symbol(symbol)
        if symbol not in self._us_native_halts:
            self.get_quote(symbol, market="US")
        return Rules(symbol=symbol, market="US", currency="USD", timezone="America/New_York",
                     lot=1, regular_open="09:30", regular_close="16:00",
                     halted=self._us_native_halts[symbol])

    def krw_order_limit(self) -> OrderLimit:
        """US pre-exchange (가환전) order limit, COSOQ02701OutBlock3.PrexchOrdAbleAmt.

        Basis: the column name 가환전주문가능금액 and the row's CrcyCode == USD only. The TR guide
        gives no unit definition and no enrollment field -- 칸 이름 근거, 실주문 확정 대기.
        Read-only; it never enrolls the account or exchanges currency.
        """
        if self.market != "US":
            raise NotSupportedError("LS pre-exchange limit is read for US")

        def read() -> OrderLimit:
            response = self._response(
                "/overseas-stock/accno", "COSOQ02701", {"COSOQ02701InBlock1": {"RecCnt": 1, "CrcyCode": "USD"}}
            )
            rows = [r for r in self._us_rows(response, "COSOQ02701OutBlock3") if r.get("CrcyCode") == "USD"]
            if len(rows) != 1:
                raise InvalidResponseError("LS pre-exchange limit requires exactly one USD row")
            return OrderLimit(decimal(rows[0]["PrexchOrdAbleAmt"]), "USD")

        return self._call(read)

    def get_balance(self) -> Balance:
        if self.market == "US":

            def read_us():
                response = self._us_balance()
                rows = self._us_rows(response, "COSOQ00201OutBlock3")
                usd = [row for row in rows if row["CrcyCode"] == "USD"]
                if len(usd) != 1:
                    raise InvalidResponseError("LS balance must contain exactly one USD row")
                # Never derive orderable USD from total assets or an FX rate.
                return Balance(
                    currency="USD",
                    cash_available=decimal(usd[0]["FcurrOrdAbleAmt"]),
                    position_value=decimal(usd[0]["FcurrEvalAmt"]),
                )

            return self._call(read_us)

        def read():
            row = self._response(
                "/stock/accno", "CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": self._code("balance_type")}}
            )["CSPAQ12200OutBlock2"]
            return Balance(cash_available=decimal(row["MnyOrdAbleAmt"]), position_value=decimal(row["BalEvalAmt"]))

        return self._call(read)

    def get_positions(self) -> list[Position]:
        if self.market == "US":

            def read_us():
                return [
                    Position(
                        symbol=self._us_symbol(row["ShtnIsuNo"]),
                        name=row["JpnMktHanglIsuNm"],
                        market="US",
                        quantity=whole(row["AstkBalQty"]),
                        sellable=whole(row["AstkSellAbleQty"]),
                        average_price=decimal(row["FcstckUprc"]),
                        last=decimal(row["OvrsScrtsCurpri"]),
                        market_value=decimal(row["FcurrEvalAmt"]),
                    )
                    for row in self._us_holdings()
                    if row["CrcyCode"] == "USD"
                ]

            return self._call(read_us)

        def read():
            body = {key: self._code(key) for key in ("prcgb", "chegb", "dangb", "charge")}
            body["cts_expcode"] = ""
            pages = PageRows(LS_KR_ROW_KEY, "LS KR")
            for response in self._pages("/stock/accno", "t0424", {"t0424InBlock": body},
                                        block="t0424OutBlock", cursor="cts_expcode"):
                pages.add(self._rows(response, "t0424OutBlock1"))
            return [
                Position(
                    symbol=self._response_symbol(row["expcode"]),
                    name=row["hname"],
                    quantity=whole(row["janqty"]),
                    sellable=whole(row["mdposqt"]),
                    average_price=decimal(row["pamt"]),
                    last=decimal(row["price"]),
                    market_value=decimal(row["appamt"]),
                )
                for row in pages.rows
            ]

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if self.market == "US":
            return self._call(lambda: self._us_orders(symbol))
        code = self._symbol(symbol, "KR") if symbol is not None else None

        def read():
            body = {
                "expcode": code if code is not None else "",
                "chegb": self._code("order_filter"),
                "medosu": self._code("side_all"),
                "sortgb": self._code("sort_order"),
                "cts_ordno": "",
            }
            # Validate before filtering: a broken page is never partial success.
            orders = [self._order(row)
                      for response in self._pages("/stock/accno", "t0425", {"t0425InBlock": body},
                                                  block="t0425OutBlock", cursor="cts_ordno")
                      for row in self._rows(response, "t0425OutBlock1")]
            return [order for order in orders if code is None or order.symbol == code]

        return self._call(read)

    def status(self, order: Order) -> Order:
        return super().status(order).model_copy(update={"virtual": self._bound_virtual})

    def _order(self, row) -> Order:
        # Official t0425 quantities; missing fields stay unknown and average fill price stays None.
        quantity = whole(row["qty"]) if self._writes_verified() and "qty" in row else None
        filled = whole(row["cheqty"]) if self._writes_verified() and "cheqty" in row else None
        remaining = whole(row["ordrem"])
        price = self._price(row.get("price"))
        if quantity is not None:
            if quantity <= 0 or remaining > quantity or (filled is not None and filled + remaining > quantity):
                raise InvalidResponseError("Inconsistent LS order quantities")
        status = "unknown"
        # No quantity subtraction, zero-remainder inference, or unverified state interpretation.
        if self._writes_verified() and quantity is not None and filled is not None:
            if filled == quantity and remaining == 0:
                status = "filled"
            elif filled > 0 and remaining > 0:
                status = "partial"
            elif filled == 0 and remaining == quantity:
                status = "open"
        return Order(
            id=self._order_id(row["ordno"]),
            symbol=self._response_symbol(row["expcode"]),
            quantity=quantity,
            price=price,
            filled=filled,
            # UNVERIFIED: cheprice is not established as the average execution price.
            fill_price=None,
            remaining=remaining,
            status=status,
            virtual=self._bound_virtual,
        )

    def _place_order(self, request: OrderRequest) -> Order:
        if self.market == "US":
            return self._us_write(request)
        body = {
            "IsuNo": self._order_symbol(request.symbol),
            "OrdQty": request.quantity,
            "OrdPrc": int(request.price or 0),
            "BnsTpCode": self._code(request.side),
            "OrdprcPtnCode": self._code(LS_SESSION_ORDER_KEYS.get(
                (request.session, request.order_type), request.order_type)),
            "MgntrnCode": self._code("cash"),
            "LoanDt": "",
            "OrdCndiTpCode": self._code("condition" if request.time_in_force == "day"
                                        else "tif_" + request.time_in_force),
            "MbrNo": request.venue or self._code("member"),
        }
        response = self._response("/stock/order", "CSPAT00601", {"CSPAT00601InBlock1": body}, write=True)
        return Order(
            id=self._order_id(response["CSPAT00601OutBlock2"]["OrdNo"]),
            symbol=self._symbol(request.symbol, "KR"),
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            virtual=self._bound_virtual,
        )

    def _cancel_order(self, request: CancelRequest) -> Order:
        if self.market == "US":
            return self._us_write(request)
        body = {
            "OrgOrdNo": request.order_id if self._bound_virtual else int(request.order_id),
            "IsuNo": self._order_symbol(request.symbol),
            "OrdQty": request.quantity,
        }
        response = self._response("/stock/order", "CSPAT00801", {"CSPAT00801InBlock1": body}, write=True)
        # UNVERIFIED: ack block; accepted does not prove the original order closed.
        self._order_id(response["CSPAT00801OutBlock2"]["OrdNo"])
        return Order(
            id=request.order_id,
            symbol=self._symbol(request.symbol, "KR"),
            status="accepted",
            virtual=self._bound_virtual,
        )

    @staticmethod
    def _us_symbol(symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol):
            raise InvalidResponseError("Invalid LS US symbol")
        return symbol

    @staticmethod
    def _us_rows(response, block):
        value = response[block]
        rows = value if isinstance(value, list) else [value]
        if any(not isinstance(row, dict) for row in rows):
            raise InvalidResponseError("Invalid LS US rows")
        return rows

    def _us_complete(self):
        # The official US TR uses response header continuation, not a body cursor.
        # An injected client must retain the last response headers; silence cannot
        # become proof that an account-wide balance or order snapshot is complete.
        headers = getattr(self._client, "response_headers", None)
        if not isinstance(headers, Mapping):
            raise SpecUnverifiedError("LS US reads require response continuation headers")
        headers = {str(k).lower(): v for k, v in headers.items()}
        if headers.get("tr_cont") != "N" or headers.get("tr_cont_key") not in ("", None):
            raise SpecUnverifiedError("LS US continued account snapshots are not implemented")

    _US_BALANCE_REQUEST = {
        "COSOQ00201InBlock1": {"RecCnt": 1, "BaseDt": "", "CrcyCode": "USD", "AstkBalTpCode": "10"},
    }

    def _us_balance(self):
        response = self._response("/overseas-stock/accno", "COSOQ00201", dict(self._US_BALANCE_REQUEST))
        self._us_complete()
        return response

    def _us_next_key(self, seen):
        # Official US continuation: response header tr_cont=Y with tr_cont_key; N with no key = last page.
        headers = self._last_headers()
        if headers is None:
            raise SpecUnverifiedError("LS US reads require response continuation headers")
        flag, key = headers.get("tr_cont"), headers.get("tr_cont_key")
        if flag == "N" and key in ("", None):
            return None
        if flag == "Y" and isinstance(key, str) and key.strip() and key.strip() not in seen:
            seen.add(key.strip())
            return key.strip()
        raise InvalidResponseError("LS US continuation headers do not show where the list ends")

    def _us_holdings(self):
        pages, seen, key = PageRows(LS_US_ROW_KEY, "LS US"), set(), None
        for index in range(LS_MAX_PAGES):
            request = dict(self._US_BALANCE_REQUEST)
            if index == 0:
                response = self._response("/overseas-stock/accno", "COSOQ00201", request)
            else:
                response = self._response("/overseas-stock/accno", "COSOQ00201", request, cont_key=key)
            pages.add(self._us_rows(response, "COSOQ00201OutBlock4"))
            key = self._us_next_key(seen)
            if key is None:
                return pages.rows
        raise InvalidResponseError("LS US holdings exceeded the page limit; refusing a truncated result")

    def _us_orders(self, symbol):
        if self._order_date is None:
            raise ConfigurationError("LS US order listing requires an explicit broker order_date")
        symbol = self._us_symbol(symbol) if symbol is not None else ""
        pages = self._pages(
            "/overseas-stock/accno",
            "COSAQ00102",
            {
                "COSAQ00102InBlock1": {
                    "RecCnt": 1,
                    "QryTpCode": "1",
                    "BkseqTpCode": "2",
                    "OrdMktCode": LS_US_VENUES[self.venue],
                    "BnsTpCode": "0",
                    "IsuNo": symbol,
                    "SrtOrdNo": 0,
                    "OrdDt": self._order_date.strftime("%Y%m%d"),
                    "ExecYn": "0",
                    "CrcyCode": "USD",
                    "ThdayBnsAppYn": "0",
                    "LoanBalHldYn": "0",
                },
            },
        )
        result = []
        rows = [row for response in pages for row in self._us_rows(response, "COSAQ00102OutBlock3")]
        for row in rows:
            code = self._us_symbol(row["ShtnIsuNo"])
            if row["CrcyCode"] != "USD" or row["OrdMktCode"] != LS_US_VENUES[self.venue]:
                raise InvalidResponseError("LS US order identity/currency does not match")
            quantity, filled, remaining = (whole(row[k]) for k in ("OrdQty", "AllExecQty", "UnercQty"))
            if not quantity or filled + remaining > quantity:
                raise InvalidResponseError("Inconsistent LS US order quantities")
            status = "filled" if filled == quantity and remaining == 0 else "unknown"
            if remaining > 0:
                status = "partial" if filled else "open"
            order = Order(
                id=self._order_id(row["OrdNo"]),
                symbol=code,
                market="US",
                quantity=quantity,
                price=self._price(row.get("OvrsOrdPrc")),
                filled=filled,
                remaining=remaining,
                fill_price=None,
                status=status,
                virtual=self._bound_virtual,
            )
            if not symbol or code == symbol:
                result.append(order)
        return result

    def _us_write(self, request):
        cancellation = isinstance(request, CancelRequest)
        body = {
            "RecCnt": 1,
            "OrdPtnCode": "08" if cancellation else {"buy": "02", "sell": "01"}[request.side],
            "OrgOrdNo": int(request.order_id) if cancellation else 0,
            "OrdMktCode": LS_US_VENUES[self.venue],
            "IsuNo": self._us_symbol(request.symbol),
            "OrdQty": request.quantity,
            "OvrsOrdPrc": 0 if cancellation else float(request.price),
            "OrdprcPtnCode": "00",
            "BrkTpCode": self._code("broker_type"),
        }
        response = self._response("/overseas-stock/order", "COSAT00301", {"COSAT00301InBlock1": body}, write=True)
        order_id = self._order_id(response["COSAT00301OutBlock2"]["OrdNo"])
        return Order(
            id=request.order_id if cancellation else order_id,
            symbol=request.symbol,
            market="US",
            status="accepted",
            virtual=self._bound_virtual,
            quantity=request.quantity,
            price=None if cancellation else request.price,
        )
