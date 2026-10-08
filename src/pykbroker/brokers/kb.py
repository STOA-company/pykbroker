"""KB B2C REST contract; production writes remain opt-in and safety-guarded.

Official source: openapi.kbsec.com/api/kbs/guide/{excel,json}/b2c,
read 2026-10-04; kbsecurities/kb-openapi @5120174652ed63cb45f062b3b6d4b9f49a0702d0.
The owner must bind supplied credentials to the requested account. This adapter
never enrolls Global One Market, converts cash, retries writes, or infers fills.
"""

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote
from pykbroker.safety.rate_limit import credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price, whole

KB_API_BASE_URL = "https://developer.kbsec.com:32484"
_US_VENUES = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMX"}
# Official XLSX input tables: sheets25/26,28,63/64. Validate the serialized
# fields at the final transport boundary; never truncate a financial request.
_KR_ORDER_LENGTHS = {"mkt_tm_clsf": 1, "is_cd": 12, "ordr_q": 10,
                     "ordr_uprc": 9, "ordr_ccd": 2, "sor_ordr_ccd": 1}
_ORDER_LENGTHS = {
    "ssam1801": _KR_ORDER_LENGTHS, "ssam1802": _KR_ORDER_LENGTHS,
    "ssam1806": {"is_cd": 12, "ordr_q": 10, "crct_clsf": 1, "orgn_ordr_no": 10},
    "skam2101": {"trd_dl_ccd": 2, "is_cd": 12, "frgn_ordr_typ_cd": 1,
                 "frgn_ordr_q": 18, "frgn_ordr_prc_p4": 15},
    "skam2102": {"crct_cncl_clsf": 1, "is_cd": 12, "orgn_ordr_no": 10, "frgn_ordr_prc_p4": 15},
}


def _check_lengths(body, limits):
    if body.keys() != limits.keys() or any(
        not isinstance(body[key], str) or not body[key] or len(body[key]) > maximum
        or "\n" in body[key] or "\r" in body[key]
        for key, maximum in limits.items()
    ):
        raise ConfigurationError("KB request fields exceed or violate the official string contract")


def _response_body(result):
    if not isinstance(result, Mapping):
        raise InvalidResponseError("KB response is not an object")
    header, data = result.get("dataHeader"), result.get("dataBody")
    if (not isinstance(header, Mapping) or header.get("resultCode") != "200"
            or header.get("processFlag") != "A"):
        raise InvalidResponseError("KB business result is unsuccessful or unknown; reconcile before retrying")
    if not isinstance(data, Mapping):
        raise InvalidResponseError("KB response body is missing")
    return data


def _price_text(price, market):
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ConfigurationError("KB limit price must be positive and finite")
    # Remove insignificant trailing zeroes without Decimal.normalize's context
    # rounding or allocating a huge fixed-point string for arbitrary exponents.
    sign, digits, exponent = price.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    precision, maximum = (0, 9) if market == "KR" else (4, 15)
    if exponent < -precision or price.adjusted() + 1 > maximum:
        raise ConfigurationError("KB limit price exceeds official precision or length")
    value = format(Decimal((sign, tuple(digits), exponent)), "f")
    if len(value) > maximum:
        raise ConfigurationError("KB limit price exceeds official string length")
    return value


class _KbRestClient:
    base_url = KB_API_BASE_URL

    def post(self, path, *, headers, body):
        request = Request(self.base_url + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=10.0) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            exc.close()
            # Do not expose broker response bodies or retry an uncertain write.
            raise InvalidResponseError("KB HTTP response refused; reconcile writes before retrying") from None


class KB(BaseBroker):
    broker_code = "kb"
    us_venues = tuple(_US_VENUES)

    def __init__(
        self, *, account: str, bound_account: str | None = None, app_key: str | None = None, token: str | None = None,
        market: str = "KR", venue: str | None = None, is_integrated: bool = False,
        writes_enabled: bool = False, client=None, order_date: date | None = None,
        symbol_map: Mapping[str, str] | None = None, client_options: Mapping | None = None, **kwargs,
    ) -> None:
        options = dict(client_options or {})
        allowed = {"bound_account", "app_key", "token", "venue", "is_integrated", "writes_enabled", "symbol_map"}
        if options.keys() - allowed:
            raise ConfigurationError("Unknown KB client_options field")
        bound_account = bound_account if bound_account is not None else options.get("bound_account")
        app_key = app_key if app_key is not None else options.get("app_key")
        token = token if token is not None else options.get("token")
        venue = venue if venue is not None else options.get("venue")
        # Financial mode flags must be explicit top-level owner arguments; environment cannot silently override them.
        if "is_integrated" in options or "writes_enabled" in options:
            raise ConfigurationError("Supply owner-bound financial mode flags explicitly")
        symbol_map = symbol_map if symbol_map is not None else options.get("symbol_map")
        super().__init__(account=account, market=market, **kwargs)
        if account != bound_account:
            raise ConfigurationError("KB credentials must be owner-bound to the requested account")
        if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value
               for value in (app_key, token)):
            raise ConfigurationError("Supply KB app_key and token")
        if type(is_integrated) is not bool or type(writes_enabled) is not bool:
            raise ConfigurationError("KB integration and write flags must be booleans")
        if market == "US" and venue not in _US_VENUES:
            raise ConfigurationError("Supply a verified US symbol venue")
        if order_date is not None and type(order_date) is not date:
            raise ConfigurationError("order_date must be a broker trading date")
        self._order_date = order_date
        self._symbol_map = dict(symbol_map or {})
        self.venue, self._is_integrated = venue, is_integrated
        self._writes_enabled = writes_enabled
        self._app_key, self._token = app_key, token
        self._client = client if client is not None else _KbRestClient()
        # Conservative local request budget; not a claim about the broker quota.
        self._limiter = credential_rate_limiter("kb", app_key, {"request": 1.0})
        self._verify_host()

    def _verify_host(self):
        if self.virtual is not False or getattr(self._client, "base_url", None) != KB_API_BASE_URL:
            raise ConfigurationError("KB client must use the official production URL")

    @staticmethod
    def issue_token(*, app_key: str, secret_key: str, virtual: bool = False, client_factory=None) -> dict:
        """Explicit Client Credentials issuance; no secret/token cache or auto-renewal.

        Official token.json and pinned auth_example.py:34-46 use the same envelope.
        Returns access_token, token_type and expires_in for caller-managed storage.
        """
        if virtual is not False:
            raise ConfigurationError("KB token issuance has no mock environment")
        body = {"grantType": "client_credentials", "appKey": app_key, "appSecret": secret_key}
        _check_lengths(body, {"grantType": 18, "appKey": 36, "appSecret": 32})
        if not app_key.strip() or not secret_key.strip():
            raise ConfigurationError("Supply KB app_key and secret_key")
        client = (client_factory or _KbRestClient)()
        if getattr(client, "base_url", None) != KB_API_BASE_URL:
            raise ConfigurationError("KB token client must use the official production URL")
        credential_rate_limiter("kb", app_key, {"request": 1.0}).wait("request")
        try:
            result = client.post("/oauth2/token", headers={"Content-Type": "application/json"},
                                 body={"dataHeader": {"ipAddr": "", "macAddr": ""}, "dataBody": body})
        except Exception:
            # An injected transport can include credentials in its exception. Never echo it.
            raise TransportError("KB token issuance failed; reconcile token state before retrying") from None
        try:
            data = _response_body(result)
            token, kind, expiry = (data[key] for key in ("access_token", "token_type", "expires_in"))
            if (not isinstance(token, str) or not token.strip() or len(token) > 440
                    or "\r" in token or "\n" in token
                    or not isinstance(kind, str) or kind.lower() != "bearer"
                    or type(expiry) is not int or not 0 < expiry < 10**10):
                raise ValueError
            return {"access_token": token, "token_type": kind, "expires_in": expiry}
        except (KeyError, TypeError, ValueError, InvalidResponseError):
            raise InvalidResponseError("KB token response is invalid") from None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True, supports_limit_order=True, supports_market_order=False,
            supports_single_order_cancel=True, markets=(self.market,),
            requires_venue=self.market == "US", venues=("KRX",) if self.market == "KR" else self.us_venues,
            supports_order_history=True, realtime_open_orders=False,
        )

    def _response(self, tr: str, body: dict) -> Mapping:
        self._verify_host()
        if tr in _ORDER_LENGTHS:
            _check_lengths(body, _ORDER_LENGTHS[tr])
        self._limiter.wait("request")
        result = self._client.post(
            "/api/v1/" + tr,
            headers={"Content-Type": "application/json", "appKey": self._app_key,
                     "Authorization": "bearer " + self._token},
            body={"dataHeader": {"ipAddr": "", "macAddr": ""}, "dataBody": body},
        )
        return _response_body(result)

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)
        def read():
            if self.market == "KR":
                row = self._response("ivu10140", {"excg_clsf": "1", "shrt_cd": symbol})
                return Quote(symbol=symbol, market=market, last=decimal(row["now_prc"]),
                             bid=optional_price(row["b_sq1_askprc"]), ask=optional_price(row["s_sq1_askprc"]),
                             prev_close=optional_price(row["bdy_cls_prc"]))
            row = self._response("gss10030", {"krx_cd": _US_VENUES[self.venue], "is_cd": symbol})
            if row.get("is_cd") != symbol or row.get("krx_cd") != _US_VENUES[self.venue]:
                raise InvalidResponseError("KB quote identity does not match the request")
            # Official JSON examples already contain decimal points; P4 is never divided again.
            return Quote(symbol=symbol, market=market, last=decimal(row["now_prc_p4"]),
                         bid=optional_price(row["b_askprc_p4"]), ask=optional_price(row["s_askprc_p4"]),
                         prev_close=optional_price(row["bdy_cls_prc_p4"]))
        return self._call(read)

    def get_rules(self, symbol: str, *, now=None, trading_day=None):
        rules = super().get_rules(symbol, now=now, trading_day=trading_day)
        if self.market == "US":
            # Official GSS10030 dl_spsn_ccd has no documented normal enum.
            # Preserve a caller's stop veto; False cannot establish native permission.
            return rules.model_copy(update={"halted": True if rules.halted is True else None})
        return rules

    def orderable_cash(self, symbol: str) -> Decimal:
        """Symbol-specific broker spending power in KRW or native foreign currency.

        Global One Market enrollment must already belong to this owner-bound account.
        It cannot be inferred from a positive amount and is never changed here.
        SKQM2106 lacks a documented returned currency/FX basis; consumers must not
        treat its foreign amount as a verified USD budget.
        """
        symbol = self._symbol(symbol, self.market)
        def read():
            if self.market == "KR":
                row = self._response("ssqm1802", {"is_no": symbol})
                return decimal(row["pcnt100_ordr_psbl_amt"])
            if not self._is_integrated:
                raise SpecUnverifiedError("KB US spending requires existing Global One Market enrollment")
            row = self._response("skqm2106", {"stnd_is_cd": symbol})
            return decimal(row["fcrncy_unty_ordr_psbl_amt_p2"])
        return self._call(read)

    def _validate_order(self, request):
        super()._validate_order(request)
        if isinstance(request, OrderRequest) and request.order_type != "limit":
            raise NotSupportedError("KB adapter currently submits limit orders only")
        if isinstance(request, OrderRequest):
            tr, body = self._order_body(request)
            _check_lengths(body, _ORDER_LENGTHS[tr])
        if self.market == "US" and isinstance(request, OrderRequest) and not self._is_integrated:
            raise SpecUnverifiedError("KB US requires existing Global One Market enrollment")
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB writes are disabled by default")
        if isinstance(request, CancelRequest):
            value = request.order_id
            if not value.isascii() or not value.isdecimal() or len(value) > 10 or not value.strip("0"):
                raise ConfigurationError("KB order number must be a nonzero decimal string of at most ten digits")
            if self.market == "KR":
                _check_lengths(self._kr_cancel_body(request), _ORDER_LENGTHS["ssam1806"])
            elif len(request.symbol) > 12:
                raise ConfigurationError("KB US order symbol exceeds twelve characters")

    @staticmethod
    def _order_number(row):
        value = row.get("ordr_no")
        if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                or len(value) > 10 or not value.strip("0")):
            raise InvalidResponseError("KB order number missing or invalid; reconcile before retrying")
        return value

    def _order_body(self, request):
        symbol = self._symbol(request.symbol, request.market)
        price = _price_text(request.price, self.market)
        if self.market == "KR":
            return ("ssam1802" if request.side == "buy" else "ssam1801"), {
                "mkt_tm_clsf": "1", "is_cd": symbol, "ordr_q": str(request.quantity),
                "ordr_uprc": price, "ordr_ccd": "00", "sor_ordr_ccd": "K",
            }
        return "skam2101", {"trd_dl_ccd": "02" if request.side == "buy" else "01", "is_cd": symbol,
                            "frgn_ordr_typ_cd": "2", "frgn_ordr_q": str(request.quantity),
                            "frgn_ordr_prc_p4": price}

    def _place_order(self, request: OrderRequest) -> Order:
        self._verify_host()
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB writes are disabled")
        tr, body = self._order_body(request)
        row = self._response(tr, body)
        if self.market == "KR":
            if row.get("is_cd") != body["is_cd"] or decimal(row["ordr_q"]) != request.quantity:
                raise InvalidResponseError("KB accepted order identity is inconsistent; reconcile before retrying")
        return Order(id=self._order_number(row), symbol=request.symbol, market=request.market,
                     side=request.side, order_type=request.order_type, quantity=request.quantity,
                     price=request.price, status="accepted", virtual=False)

    @staticmethod
    def _rows(row, block, count):
        rows = row.get(block)
        if (not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows)
                or whole(row[count]) != len(rows)):
            raise InvalidResponseError("KB account or order row count is inconsistent")
        return rows

    @staticmethod
    def _funding_rows(data):
        """SKQM3350's array(10) includes zero-filled padding, unlike ledger pages."""
        rows = data.get("Record1")
        if not isinstance(rows, list) or len(rows) > 10 or any(not isinstance(row, Mapping) for row in rows):
            raise InvalidResponseError("KB funding rows are invalid")
        active = []
        for row in rows:
            currency = row.get("crncy_cd")
            if not isinstance(currency, str):
                raise InvalidResponseError("KB funding currency is missing")
            if currency.strip():
                active.append(row)
            else:
                # A missing identity with money is corruption, never harmless padding.
                amounts = ("fcrncy_rcvbl_ordr_psbl_amt_p2", "fcrncy_tfnd", "fcrncy_use_psbl_amt_p2",
                           "fcrncy_unty_ordr_psbl_amt_p2")
                if (not isinstance(row.get("ntn_nm"), str) or row["ntn_nm"].strip()
                        or any(decimal(row[key]) != 0 for key in amounts)):
                    raise InvalidResponseError("KB funding padding contains unidentified data")
        if whole(data["grid_cnt"]) != len(active):
            raise InvalidResponseError("KB funding row count is inconsistent")
        return active

    def _pages(self, tr, body, block, count):
        cursor, seen = "", set()
        for _ in range(100):
            page = self._response(tr, {**body, "nxt_key": cursor})
            if tr == "spqm2226" and page.get(block) == [] and whole(page[count]) != 0:
                # Official SPQM2226 export contains a displaced holding in Record1,
                # positive valuation and no Record2. A final-key/message is not
                # permission to silently erase that holding. Do not reconstruct it.
                raise SpecUnverifiedError("KB foreign holdings response is incomplete; verify the native row block")
            yield self._rows(page, block, count)
            cursor = page.get("nxt_key")
            if not isinstance(cursor, str):
                raise InvalidResponseError("KB continuation evidence is missing")
            cursor = cursor.strip()
            if not cursor:
                return
            if cursor in seen:
                raise InvalidResponseError("KB continuation key repeated")
            seen.add(cursor)
        raise InvalidResponseError("KB account/order pagination exceeded its bounded budget")

    def _response_symbol(self, value, market):
        if not isinstance(value, str) or not value.strip():
            raise InvalidResponseError("KB response symbol missing")
        raw = value.strip()
        mapped = self._symbol_map.get(raw, raw)
        try:
            return self._symbol(mapped, market)
        except ConfigurationError:
            if market != "KR" or len(raw) != 12 or not raw.isascii() or not raw.isalnum():
                raise SpecUnverifiedError("KB response instrument mapping is unverified") from None
        # Official SIQM4900 accepts standard IDs and returns both standard and short codes.
        # Bind the native result exactly; never slice an ISIN into a ticker.
        row = self._response("siqm4900", {"stnd_is_cd": raw})
        standard = row.get("stnd_is_cd")
        if not isinstance(standard, str) or standard.strip() != raw:
            raise InvalidResponseError("KB instrument mapping identity differs from the ledger")
        try:
            mapped = self._symbol(row.get("shrt_is_cd"), "KR")
        except ConfigurationError:
            raise InvalidResponseError("KB instrument mapping short code is invalid") from None
        self._symbol_map[raw] = mapped
        return mapped

    def get_positions(self) -> list[Position]:
        def read():
            if self.market == "KR":
                data = self._response("ssqm2952", {"excg_mktpr_ccd": "K"})
                rows = self._rows(data, "Record1", "grid_cnt1")
                if whole(data["tl_data_cnt"]) != len(rows):
                    raise SpecUnverifiedError("KB KR holdings are truncated; no continuation input is documented")
                result = []
                for row in rows:
                    currency = row["crncy_cd"].strip()
                    if currency not in {"", "KRW"}:
                        continue
                    # SSQM2952 is execution-based: ec_q includes unsettled buys/sells.
                    # Official sample has hld_q=0, ec_q=1. These are not additive.
                    quantity = whole(row["ec_q"])
                    if not quantity:
                        continue
                    result.append(Position(
                        symbol=self._response_symbol(row["is_cd"], "KR"), name=row["is_nm"], market="KR",
                        quantity=quantity, sellable=whole(row["ordr_psbl_q"]),
                        average_price=decimal(row["byng_avr_prc"]), last=decimal(row["now_prc"]),
                        market_value=decimal(row["val_amt"]),
                    ))
                return result
            result = []
            for page in self._pages("spqm2226", {"std_crncy_f": "1"}, "Record2", "grid_cnt2"):
                for row in page:
                    if row.get("crncy_clsf_nm", "").strip() != "USD":
                        if decimal(row["frgn_hld_q_p6"]) > 0:
                            raise SpecUnverifiedError("KB foreign holding currency is not verified as USD")
                        continue
                    quantity = whole(row["frgn_hld_q_p6"])
                    if not quantity:
                        continue
                    last = decimal(row["now_prc_p4"])
                    result.append(Position(
                        symbol=self._response_symbol(row["is_cd"], "US"), name=row["is_nm"], market="US",
                        quantity=quantity, sellable=whole(row["frgn_ordr_psbl_q_p6"]),
                        average_price=decimal(row["byng_avr_prc_p4"]), last=last, market_value=last * quantity,
                    ))
            return result
        return self._call(read)

    def get_balance(self) -> Balance:
        def read():
            if self.market == "KR":
                cash = decimal(self._response("ssqm0004", {"is_no": ""})["ordr_psbl_csh"])
            else:
                data = self._response("skqm3350", {})
                rows = self._funding_rows(data)
                usd = [row for row in rows if row.get("crncy_cd", "").strip() == "USD"]
                if len(usd) != 1:
                    raise InvalidResponseError("KB funding requires one explicitly identified USD row")
                cash = min(decimal(usd[0]["fcrncy_tfnd"]), decimal(usd[0]["fcrncy_use_psbl_amt_p2"]))
            positions = self.get_positions()
            return Balance(currency="KRW" if self.market == "KR" else "USD", cash_available=cash,
                           position_value=sum((item.market_value for item in positions), Decimal(0)))
        return self._call(read)

    def _ledger_date(self):
        if self._order_date is not None:
            return self._order_date.strftime("%Y%m%d")
        # SPQM2204 sheet71 does not define the date's timezone. Preserve the
        # existing default; callers can supply the verified broker order_date.
        zone = "Asia/Seoul" if self.market == "KR" else "America/New_York"
        return datetime.now(ZoneInfo(zone)).strftime("%Y%m%d")

    @staticmethod
    def _quantity_status(quantity, filled, remaining):
        if filled > quantity or remaining > quantity or filled + remaining > quantity:
            raise InvalidResponseError("KB ledger quantities are inconsistent")
        if quantity > 0 and filled == quantity and remaining == 0:
            return "filled"
        if 0 < filled < quantity and remaining > 0:
            return "partial"
        # Zero remaining can mean rejection/cancellation; absence is never a fill.
        return "unknown"

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        wanted = self._symbol(symbol, self.market) if symbol is not None else None
        def read():
            day = self._ledger_date()
            if self.market == "KR":
                pages = self._pages("ssqm2341", {"ccls_clsf": "0", "ordr_dt": day}, "Record1", "grid_cnt1")
            else:
                pages = self._pages("spqm2204", {"strt_ordr_dt": day, "end_ordr_dt": day,
                                               "ccls_clsf": "0", "trd_clsf": "99", "dl_clsf": "1"},
                                    "Record1", "grid_cnt")
            result, seen = [], set()
            for page in pages:
                for row in page:
                    if self.market == "KR":
                        code = self._response_symbol(row["stnd_is_no"], "KR")
                        quantity, filled, remaining = (whole(row[key]) for key in ("ordr_q", "tl_ccls_q", "nccls_q"))
                        price, fill_price = optional_price(row["ordr_uprc"]), optional_price(row["ccls_uprc"])
                        side = None  # Display-name strings have no verified enum contract.
                    else:
                        if row.get("crncy_cd", "").strip() != "USD" or row.get("frex_ccd") not in _US_VENUES.values():
                            continue
                        code = self._response_symbol(row["shrt_is_cd"], "US")
                        quantity, filled, remaining = (whole(row[key]) for key in (
                            "frgn_ordr_q_p6", "ccls_q_p6", "nccls_q_p6"))
                        price = optional_price(row["frgn_ordr_prc_p6"])
                        fill_price = optional_price(row["frgn_ccls_prc_p6"])
                        side = {"01": "sell", "02": "buy"}.get(row.get("etc_trd_ccd"))
                    if wanted is not None and code != wanted:
                        continue
                    number = self._order_number(row)
                    if number in seen:
                        raise InvalidResponseError("KB ledger repeats an order identity")
                    seen.add(number)
                    status = self._quantity_status(quantity, filled, remaining)
                    result.append(Order(id=number, symbol=code, market=self.market, side=side, quantity=quantity,
                                        price=price, filled=filled, remaining=remaining,
                                        fill_price=fill_price if filled == 1 else None, status=status))
            return result
        return self._call(read)

    def _kr_cancel_body(self, request):
        # sheet28:13 calls 1/2 partial/full correction, but SSAM1806.json
        # explicitly demonstrates cancellation with 1 + quantity. Follow that
        # successful quantity-specific example; never infer a full-cancel enum.
        return {"is_cd": self._symbol(request.symbol, request.market), "ordr_q": str(request.quantity),
                "crct_clsf": "1", "orgn_ordr_no": request.order_id}

    def _cancel_order(self, request: CancelRequest) -> Order:
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB cancellation writes disabled")
        if self.market == "KR":
            body = self._kr_cancel_body(request)
            row = self._response("ssam1806", body)
            if row.get("orgn_ordr_no") != request.order_id or row.get("is_cd") != body["is_cd"]:
                raise InvalidResponseError("KB cancellation identity inconsistent; reconcile before retrying")
        else:
            orders = [item for item in self.list_orders(request.symbol) if item.id == request.order_id]
            if len(orders) != 1 or orders[0].remaining != request.quantity or orders[0].price is None:
                raise SpecUnverifiedError("KB US cancellation requires exact current full remainder and original price")
            price = _price_text(orders[0].price, "US")
            # SKAM2102 exposes no cancel quantity: a partial-quantity request must not cancel the entire order.
            row = self._response("skam2102", {"crct_cncl_clsf": "2", "is_cd": request.symbol,
                                             "orgn_ordr_no": request.order_id,
                                             "frgn_ordr_prc_p4": price})
        self._order_number(row)
        # Acknowledgement is not proof that any original remainder was actually cancelled.
        return Order(id=request.order_id, symbol=request.symbol, market=request.market, status="unknown")
