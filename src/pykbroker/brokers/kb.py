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
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, NotSupportedError, SpecUnverifiedError
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote
from pykbroker.safety.rate_limit import credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price, whole

KB_API_BASE_URL = "https://developer.kbsec.com:32484"
_US_VENUES = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMX"}


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

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True, supports_limit_order=True, supports_market_order=False,
            supports_single_order_cancel=True, markets=(self.market,),
            requires_venue=self.market == "US", venues=("KRX",) if self.market == "KR" else self.us_venues,
            supports_order_history=True, realtime_open_orders=False,
        )

    def _response(self, tr: str, body: dict) -> Mapping:
        self._verify_host()
        self._limiter.wait("request")
        result = self._client.post(
            "/api/v1/" + tr,
            headers={"Content-Type": "application/json", "appKey": self._app_key,
                     "Authorization": "bearer " + self._token},
            body={"dataHeader": {"ipAddr": "", "macAddr": ""}, "dataBody": body},
        )
        if not isinstance(result, Mapping):
            raise InvalidResponseError("KB response is not an object")
        header, data = result.get("dataHeader"), result.get("dataBody")
        if not isinstance(header, Mapping) or header.get("resultCode") != "200" or header.get("processFlag") != "A":
            raise InvalidResponseError("KB business result is unsuccessful or unknown; reconcile before retrying")
        if not isinstance(data, Mapping):
            raise InvalidResponseError("KB response body is missing")
        return data

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

    def orderable_cash(self, symbol: str) -> Decimal:
        """Symbol-specific broker spending power (KRW or USD), never fabricated FX cash.

        Global One Market enrollment must already belong to this owner-bound account.
        It cannot be inferred from a positive amount and is never changed here.
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
        if self.market == "US" and isinstance(request, OrderRequest) and request.price * 10000 != (
            request.price * 10000
        ).to_integral_value():
            raise ConfigurationError("KB US limit price supports at most four decimal places")
        if self.market == "US" and not self._is_integrated:
            raise SpecUnverifiedError("KB US requires existing Global One Market enrollment")
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB writes are disabled by default")
        if isinstance(request, CancelRequest):
            value = request.order_id
            if not value.isascii() or not value.isdecimal() or len(value) > 10 or not value.strip("0"):
                raise ConfigurationError("KB order number must be a nonzero decimal string of at most ten digits")

    @staticmethod
    def _order_number(row):
        value = row.get("ordr_no")
        if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                or len(value) > 10 or not value.strip("0")):
            raise InvalidResponseError("KB order number missing or invalid; reconcile before retrying")
        return value

    def _place_order(self, request: OrderRequest) -> Order:
        self._verify_host()
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB writes are disabled")
        if self.market == "KR":
            tr = "ssam1802" if request.side == "buy" else "ssam1801"
            row = self._response(tr, {"mkt_tm_clsf": "1", "is_cd": request.symbol,
                                     "ordr_q": str(request.quantity), "ordr_uprc": str(request.price),
                                     "ordr_ccd": "00", "sor_ordr_ccd": "K"})
            if row.get("is_cd") != request.symbol or decimal(row["ordr_q"]) != request.quantity:
                raise InvalidResponseError("KB accepted order identity is inconsistent; reconcile before retrying")
        else:
            row = self._response("skam2101", {"trd_dl_ccd": "02" if request.side == "buy" else "01",
                                            "is_cd": request.symbol, "frgn_ordr_typ_cd": "2",
                                            "frgn_ordr_q": str(request.quantity),
                                            "frgn_ordr_prc_p4": format(request.price, "f")})
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

    def _pages(self, tr, body, block, count):
        cursor, seen = "", set()
        for _ in range(100):
            page = self._response(tr, {**body, "nxt_key": cursor})
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
            # Standard instrument numbers are not converted to tickers by guessing.
            raise SpecUnverifiedError("Supply the existing verified standard-id-to-symbol mapping") from None

    def get_positions(self) -> list[Position]:
        def read():
            if self.market == "KR":
                data = self._response("ssqm2952", {"excg_mktpr_ccd": "K"})
                rows = self._rows(data, "Record1", "grid_cnt1")
                if whole(data["tl_data_cnt"]) != len(rows):
                    raise SpecUnverifiedError("KB KR holdings are truncated; no continuation input is documented")
                result = []
                for row in rows:
                    if row.get("crncy_cd", "").strip() != "KRW":
                        continue
                    quantity = whole(row["hld_q"])
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
                rows = self._rows(data, "Record1", "grid_cnt")
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

    def _cancel_order(self, request: CancelRequest) -> Order:
        if not self._writes_enabled:
            raise SpecUnverifiedError("KB cancellation writes disabled")
        if self.market == "KR":
            row = self._response("ssam1806", {"is_cd": request.symbol, "ordr_q": str(request.quantity),
                                             "crct_clsf": "1", "orgn_ordr_no": request.order_id})
            if row.get("orgn_ordr_no") != request.order_id or row.get("is_cd") != request.symbol:
                raise InvalidResponseError("KB cancellation identity inconsistent; reconcile before retrying")
        else:
            orders = [item for item in self.list_orders(request.symbol) if item.id == request.order_id]
            if len(orders) != 1 or orders[0].remaining != request.quantity or orders[0].price is None:
                raise SpecUnverifiedError("KB US cancellation requires exact current full remainder and original price")
            if orders[0].price * 10000 != (orders[0].price * 10000).to_integral_value():
                raise SpecUnverifiedError("KB original US price is not representable by the cancellation contract")
            # SKAM2102 exposes no cancel quantity: a partial-quantity request must not cancel the entire order.
            row = self._response("skam2102", {"crct_cncl_clsf": "2", "is_cd": request.symbol,
                                             "orgn_ordr_no": request.order_id,
                                             "frgn_ordr_prc_p4": format(orders[0].price, "f")})
        self._order_number(row)
        # Acknowledgement is not proof that any original remainder was actually cancelled.
        return Order(id=request.order_id, symbol=request.symbol, market=request.market, status="unknown")
