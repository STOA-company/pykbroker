"""KB B2C REST contract; production writes remain opt-in and safety-guarded.

Official source: openapi.kbsec.com/api/kbs/guide/{excel,json}/b2c,
read 2026-10-04; kbsecurities/kb-openapi @5120174652ed63cb45f062b3b6d4b9f49a0702d0.
The owner must bind supplied credentials to the requested account. This adapter
never enrolls Global One Market, converts cash, retries writes, or infers fills.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request

from pykbroker._http import urlopen
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, NotSupportedError, SpecUnverifiedError
from pykbroker.models import CancelRequest, Capabilities, Order, OrderRequest, Quote
from pykbroker.safety.rate_limit import credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price

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
        self, *, account: str, bound_account: str, app_key: str, token: str,
        market: str = "KR", venue: str | None = None, is_integrated: bool = False,
        writes_enabled: bool = False, client=None, **kwargs,
    ) -> None:
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
            supports_single_order_cancel=self.market == "KR", markets=(self.market,),
            requires_venue=self.market == "US", venues=("KRX",) if self.market == "KR" else self.us_venues,
            supports_order_history=False, realtime_open_orders=False,
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

    def _cancel_order(self, request: CancelRequest) -> Order:
        if not self._writes_enabled or self.market != "KR":
            raise SpecUnverifiedError("KB cancellation contract unavailable or writes disabled")
        row = self._response("ssam1806", {"is_cd": request.symbol, "ordr_q": str(request.quantity),
                                         "crct_clsf": "1", "orgn_ordr_no": request.order_id})
        self._order_number(row)
        if row.get("orgn_ordr_no") != request.order_id or row.get("is_cd") != request.symbol:
            raise InvalidResponseError("KB cancellation identity inconsistent; reconcile before retrying")
        # Cancellation acknowledgement does not prove the original order is cancelled.
        return Order(id=request.order_id, symbol=request.symbol, market=request.market, status="unknown")
