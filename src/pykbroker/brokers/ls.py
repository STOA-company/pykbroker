"""LS KR/US REST mappings with an injected boundary and default-closed writes.

Field/path source: official TR guide version 20261002071141740 (2026-10-04).
Official public live-client code establishes all-zero rsp_cd and per-TR US read
successes; COSAT00301 detail supplies a blank BrkTpCode in its Python example.
Persisted account tokens use the shared no-redirect transport/rate limiter.
Real writes default OFF, require explicit opt-in, broker account identity read,
and OrderSafety. Mock REST binding, unified margin enrollment, average fill
price and continued US snapshots remain unverified; no token issuer is added.
"""

import json
import re
import urllib.request
from collections.abc import Mapping
from datetime import date

from pykbroker._http import urlopen
from pykbroker.brokers._common import BaseBroker, PageRows, decimal, optional_price, whole
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.experimental._rest import RestBroker
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import equity_rules
from pykbroker.safety.rate_limit import credential_rate_limiter

# Field contracts: official public guide version 20261002071141740.
# https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
LS_READ_SUCCESS = {"COSAQ00102": {"00136"}, "COSOQ00201": {"00001"}}
LS_RATES = {
    "t1101": 10,
    "t1102": 10,
    "CSPAQ12200": 1,
    "t0424": 2,
    "t0425": 2,
    "CSPAT00601": 10,
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
}
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
    """One POST attempt through the shared no-redirect boundary; no token issue/refresh."""

    def __init__(self):
        self.base_url = LS_API_BASE_URL
        self.response_headers = {}

    def post(self, path, *, headers, json):
        self.response_headers = {}
        request = urllib.request.Request(self.base_url + path, data=_json_bytes(json), headers=headers, method="POST")
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
        **kwargs,
    ) -> None:
        if not isinstance(spec_verified, bool) or not isinstance(is_integrated, bool):
            raise ConfigurationError("LS verification and margin flags must be booleans")
        if type(allow_live_orders) is not bool:
            raise ConfigurationError("allow_live_orders must be a boolean")
        if is_integrated:
            raise SpecUnverifiedError("LS unified margin enrollment/settlement contract is unverified")
        if market == "US" and venue not in LS_US_VENUES:
            raise ConfigurationError("LS US venue must be NYSE or NASDAQ")
        if order_date is not None and type(order_date) is not date:
            raise ConfigurationError("LS order_date must be an explicit date")
        client, codes = kwargs.pop("client", None), kwargs.pop("codes", None)
        headers, limiter = kwargs.pop("headers", None), kwargs.pop("rate_limiter", None)
        options = dict(client_options or {})
        if set(options) - {"app_key", "token", "token_type", "mac_address"}:
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
            # UNVERIFIED: t0425 is not yet proof of real-time/history coverage.
            supports_order_history=False,
            realtime_open_orders=False,
            markets=(self.market,),
            requires_venue=self.market == "US",
            venues=("KRX",) if self.market == "KR" else self.us_venues,
        )

    def _validate_order(self, request: OrderRequest | CancelRequest) -> None:
        super()._validate_order(request)
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
        self._verify_mode(self._client)
        self._call(self._verify_account)
        return super().place_order(request)

    def cancel_order(self, request: CancelRequest) -> Order:
        if not self._writes_verified():
            raise SpecUnverifiedError("LS writes require the explicit real-account opt-in or verified mock")
        self._verify_mode(self._client)
        self._call(self._verify_account)
        return super().cancel_order(request)

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

    def _continued_request(self, path, tr, body, cont_key):
        # Official TR guide request headers: tr_cont=Y and the tr_cont_key the previous response returned.
        if self._client is None or self._limiter is None:
            raise ConfigurationError("Supply a REST client and TR rate limiter")
        self._limiter.wait(tr)
        headers = {**self._headers, "content-type": "application/json", "tr_cd": tr,
                   "tr_cont": "Y", "tr_cont_key": cont_key}
        return self._client.post(path, headers=headers, json=body)

    def _last_headers(self):
        headers = getattr(self._client, "response_headers", None)
        if not isinstance(headers, Mapping):
            return None
        return {str(k).lower(): v for k, v in headers.items()}

    def _response(self, path, tr, body, *, write=False, cont_key=None):
        self._verify_mode(self._client)
        if cont_key is None:
            result = self._request(path, tr, body)
        else:
            result = self._continued_request(path, tr, body, cont_key)
        if not isinstance(result, dict):
            raise InvalidResponseError("Invalid LS response; reconcile writes before retrying")
        code = result.get("rsp_cd")
        if not isinstance(code, str) or not code.strip():
            raise InvalidResponseError("Missing LS response code; reconcile writes before retrying")
        # Official token-present callRestTr and isApiSuccessCode; never the demo generator.
        # Extra codes are documented in that client's TR_META for the two US reads.
        if not re.fullmatch("0+", code) and (write or code not in LS_READ_SUCCESS.get(tr, set())):
            if write:
                # Error meanings are unverified: never release a write for retry.
                raise InvalidResponseError("Unverified LS write result; reconcile before retrying")
            raise BrokerRejectedError("LS rejected the read request")
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

    def _page_cursor(self, response, block, cursor):
        # Official t0424 contract: OutBlock.cts_expcode is copied into the next InBlock; blank = last page.
        meta = response[block]
        if not isinstance(meta, dict) or cursor not in meta or not isinstance(meta[cursor], str):
            raise InvalidResponseError("Missing LS continuation evidence")
        value = meta[cursor].strip()
        headers = self._last_headers()
        if not value and headers is not None and headers.get("tr_cont") == "Y":
            raise InvalidResponseError("LS continuation signals conflict")
        return value

    def _kr_cont_key(self):
        key = (self._last_headers() or {}).get("tr_cont_key")
        return key.strip() if isinstance(key, str) else ""

    @staticmethod
    def _complete_page(response, block, cursor):
        # UNVERIFIED: continuation; never return a partial account snapshot.
        meta = response[block]
        if not isinstance(meta, dict) or cursor not in meta or not isinstance(meta[cursor], str):
            raise InvalidResponseError("Missing LS continuation evidence")
        if meta[cursor].strip():
            raise SpecUnverifiedError("LS pagination has not been verified")

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

        def read_kr():
            # Official t1101 주식현재가호가조회 (TR guide 20261026021444748, /stock/market-data):
            # offerho1=매도호가1, bidho1=매수호가1, jnilclose=전일종가. KRX book only (no exchgubun input).
            row = self._response("/stock/market-data", "t1101", {"t1101InBlock": {"shcode": symbol}})["t1101OutBlock"]
            if row.get("shcode") not in (None, symbol):
                raise InvalidResponseError("LS quote symbol does not match request")
            bid, ask = self._price(row.get("bidho1")), self._price(row.get("offerho1"))
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None  # a crossed pair is no tradable spread
            return Quote(symbol=symbol, last=decimal(row["price"]), bid=bid, ask=ask,
                         prev_close=self._price(row.get("jnilclose")))

        return self._call(read_kr)

    def get_rules(self, symbol, *, now=None, trading_day=None):
        if self.market != "US":
            code = self._symbol(symbol, "KR")

            def read_kr():
                # Official t1102 (TR guide 20261026021444748): uplmtprice=상한가, dnlmtprice=하한가.
                # Missing/zero sides stay unknown; never derive a band from 기준가.
                row = self._response(
                    "/stock/market-data", "t1102",
                    {"t1102InBlock": {"shcode": code, "exchgubun": self._code("exchange")}},
                )["t1102OutBlock"]
                last = decimal(row["price"])
                upper, lower = self._price(row.get("uplmtprice")), self._price(row.get("dnlmtprice"))
                if upper is None or lower is None:
                    upper = lower = None
                elif not lower <= last <= upper:
                    raise InvalidResponseError("LS daily band does not contain the last price")
                return equity_rules(code, last, halted=self._halt_from_provider(code), now=now,
                                    trading_day=trading_day, limit_up=upper, limit_down=lower)

            return self._call(read_kr)
        symbol = self._us_symbol(symbol)
        if symbol not in self._us_native_halts:
            self.get_quote(symbol, market="US")
        return Rules(symbol=symbol, market="US", currency="USD", timezone="America/New_York",
                     lot=1, regular_open="09:30", regular_close="16:00",
                     halted=self._us_native_halts[symbol])

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
            base = {key: self._code(key) for key in ("prcgb", "chegb", "dangb", "charge")}
            pages, cursor, seen = PageRows(LS_KR_ROW_KEY, "LS KR"), "", set()
            for index in range(LS_MAX_PAGES):
                request = {"t0424InBlock": {**base, "cts_expcode": cursor}}
                if index == 0:
                    response = self._response("/stock/accno", "t0424", request)
                else:
                    response = self._response("/stock/accno", "t0424", request, cont_key=self._kr_cont_key())
                pages.add(self._rows(response, "t0424OutBlock1"))
                cursor = self._page_cursor(response, "t0424OutBlock", "cts_expcode")
                if not cursor:
                    break
                if cursor in seen:
                    raise InvalidResponseError("LS continuation key repeated")
                seen.add(cursor)
            else:
                raise InvalidResponseError("LS holdings exceeded the page limit; refusing a truncated result")
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
            response = self._response("/stock/accno", "t0425", {"t0425InBlock": body})
            self._complete_page(response, "t0425OutBlock", "cts_ordno")
            # Validate before filtering: a broken page is never partial success.
            orders = [self._order(row) for row in self._rows(response, "t0425OutBlock1")]
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
            "IsuNo": self._symbol(request.symbol, "KR"),
            "OrdQty": request.quantity,
            "OrdPrc": int(request.price or 0),
            "BnsTpCode": self._code(request.side),
            "OrdprcPtnCode": self._code(request.order_type),
            "MgntrnCode": self._code("cash"),
            "LoanDt": "",
            "OrdCndiTpCode": self._code("condition"),
            "MbrNo": self._code("member"),
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
            "IsuNo": self._symbol(request.symbol, "KR"),
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
        response = self._response(
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
        self._us_complete()
        result = []
        for row in self._us_rows(response, "COSAQ00102OutBlock3"):
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
