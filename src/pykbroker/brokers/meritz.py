"""Meritz REST translation. Pinned official sources: fixtures/meritz/official/CONTRACT-2026-10-25.md.

No token refresh, warning confirmation, order retry or inferred execution. The
caller binds the app-key account and supplies the broker trading date for US mutations.
"""

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo

from pykbroker._http import urlopen
from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import AmendRequest, Balance, CancelRequest, Capabilities, Order, Position, Quote
from pykbroker.models.markets import CURRENCIES
from pykbroker.reject_codes import rejection_for_code
from pykbroker.safety.rate_limit import credential_rate_limiter

from ._common import BaseBroker, decimal, optional_price, whole

MERITZ_API_BASE_URL = "https://openapi.imeritz.com:9443"
MERITZ_MOCK_BASE_URL = None
KR_KINDS = {"limit": "01", "market": "05", "conditional_limit": "06", "best": "12",
            "priority": "13", "stop_limit": "83", "midpoint": "84", "close": "71"}
KR_VENUES = {"KRX": "01", "NXT": "02", "SOR": "03"}
US_VENUES = {"NASDAQ": ("0537", "OQ"), "NYSE": ("0321", "NY"), "AMEX": ("0066", "AX")}
TIF = {"day": "0", "ioc": "3", "fok": "4"}
US_KINDS = {"limit": "01", "market": "05", "loo": "31", "loc": "32", "moo": "33", "moc": "34"}
OVERSEAS_CONDITIONS = {"limit": "0", "market": "0", "loo": "2", "loc": "7", "moo": "2", "moc": "7"}
# Official sell request ovrs_exch_vndr_idno; quote suffixes only where documented.
MARKET_VENUES = {"US": US_VENUES, "HK": {"HKEX": ("0104", "HK")},
                 "CN_SH": {"SSE": ("0215", "SH")}, "CN_SZ": {"SZSE": ("0214", "SZ")},
                 "JP_TSE": {"TSE": ("0106", "JP")}, "JP_OSE": {"OSE": ("0107", None)},
                 "JP_JASDAQ": {"JASDAQ": ("0112", None)},
                 "VN_HOSE": {"HOSE": ("0770", None)}, "VN_HNX": {"HNX": ("0771", None)}}
MARKET_CODES = {"US": "01", "HK": "02", "CN_SH": "03", "CN_SZ": "04",
                "JP_TSE": "05", "JP_OSE": "05", "JP_JASDAQ": "05", "VN_HOSE": "24", "VN_HNX": "24"}
SHARED_MARKETS = {market for market, code in MARKET_CODES.items() if list(MARKET_CODES.values()).count(code) > 1}
US_ASPR = {"1": "market", "2": "limit", "4": "loo", "5": "loc", "6": "moo", "7": "moc"}
DATA_SUCCESS_PATHS = {"/market/v1/prices"}  # corrections.json: market_prices degraded rsp_cd
SUCCESS = {"0000", "0001", "5762", "5766", "5820", "5822"}


def _text(value, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ConfigurationError("Invalid Meritz credential or header field")
    return value


def _number_id(value):
    if isinstance(value, bool):
        raise InvalidResponseError("Invalid Meritz order number")
    n = whole(value)
    if n <= 0:
        raise InvalidResponseError("Missing Meritz order number")
    return str(n)


class _MeritzRestClient:
    base_url = MERITZ_API_BASE_URL

    def request(self, method, path, *, headers, params=None, body=None, form=False):
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        data = None if body is None else (urlencode(body) if form else json.dumps(body)).encode()
        req = Request(url, data=data, headers=headers, method=method)
        response = None
        failed = False
        try:
            try:
                response = urlopen(req, timeout=15)
            except HTTPError as exc:
                response = exc  # documented refusals arrive as HTTP 500; inspect JSON code first
            return json.loads(response.read(), parse_float=Decimal)
        except Exception:
            failed = True
        finally:
            if response is not None:
                response.close()
        if failed:
            raise TransportError("Meritz transport failed; reconcile before retrying") from None


class Meritz(BaseBroker):
    broker_code = "meritz"
    supported_markets = ("KR", *MARKET_VENUES)
    us_venues = tuple(US_VENUES)
    mock_base_url = MERITZ_MOCK_BASE_URL

    def __init__(self, *, account, bound_account=None, app_key=None, token=None, mac_address=None,
                 market="KR", venue=None, client=None, order_date=None, writes_enabled=False,
                 client_options=None, **kwargs):
        options = dict(client_options or {})
        if options.keys() - {"bound_account", "app_key", "token", "mac_address", "venue"}:
            raise ConfigurationError("Unknown Meritz client_options field")
        super().__init__(account=account, market=market, **kwargs)
        bound_account = bound_account if bound_account is not None else options.get("bound_account")
        if bound_account != account:
            raise ConfigurationError("Meritz credentials must be owner-bound to the requested account")
        self._app_key = _text(app_key if app_key is not None else options.get("app_key"), 36)
        self._token = _text(token if token is not None else options.get("token"), 1000)
        self._mac = _text(mac_address if mac_address is not None else options.get("mac_address"), 12)
        if len(self._mac) != 12 or any(c not in "0123456789ABCDEF" for c in self._mac):
            raise ConfigurationError("Meritz MAC must be twelve uppercase hexadecimal characters")
        self.venue = venue if venue is not None else options.get("venue")
        if market != "KR" and self.venue not in MARKET_VENUES[market]:
            raise ConfigurationError("Supply a documented venue for this market")
        if type(writes_enabled) is not bool or (order_date is not None and type(order_date) is not date):
            raise ConfigurationError("Invalid Meritz write flag or order date")
        self._writes_enabled, self._order_date = writes_enabled, order_date
        self._client = client if client is not None else _MeritzRestClient()
        self._limiter = credential_rate_limiter("meritz", self._app_key, {})
        self._verify_host()

    def _verify_host(self):
        if self.virtual is not False or self._client.base_url != MERITZ_API_BASE_URL:
            raise ConfigurationError("Meritz has only the documented production domain")

    @staticmethod
    def issue_token(*, app_key, secret_key, client_factory=None, virtual=False):
        if virtual is not False:
            raise NotSupportedError("Meritz has no documented mock token domain")
        body = {"grant_type": "client_credentials", "client_id": _text(app_key, 36),
                "client_secret": _text(secret_key, 36), "scope": "login"}
        client = (client_factory or _MeritzRestClient)()
        if client.base_url != MERITZ_API_BASE_URL:
            raise ConfigurationError("Invalid Meritz token host")
        failed = False
        try:
            data = client.request("POST", "/oauth2/token",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body, form=True)
        except Exception:
            failed = True
        if failed:
            raise TransportError("Meritz token issuance failed") from None
        invalid = False
        try:
            token = _text(data["access_token"], 1000)
            expiry = whole(data["expires_in"])  # schema string, official example integer
            if data["token_type"].lower() != "bearer" or expiry <= 0:
                raise ValueError
        except Exception:
            invalid = True
        if invalid:
            raise InvalidResponseError("Invalid Meritz token response") from None
        return {"access_token": token, "token_type": "Bearer", "expires_in": expiry}

    def capabilities(self):
        kr = self.market == "KR"
        return Capabilities(supports_spot=True, supports_market_order=True, supports_single_order_cancel=True,
                            supports_amend=True, supports_order_history=True, markets=(self.market,),
                            supports_nxt=kr, requires_venue=not kr,
                            venues=tuple(KR_VENUES) if kr else tuple(MARKET_VENUES[self.market]),
                            order_kinds=tuple(KR_KINDS) if kr else tuple(US_KINDS),
                            time_in_force=tuple(TIF) if kr else ("day",),
                            sessions=("regular", "pre", "after") if kr else ("regular",))

    def _response(self, method, path, *, body=None, params=None, write=False):
        self._verify_host()
        if not self._token:
            raise ConfigurationError("Meritz token discarded; explicitly issue a new token")
        self._limiter.register({path: 10.0})
        self._limiter.wait(path)
        failed = False
        try:
            data = self._client.request(method, path, headers={"Content-Type": "application/json",
                                        "Authorization": "Bearer " + self._token, "mac_address": self._mac},
                                        body=body, params=params)
        except Exception:
            failed = True
        if failed:
            raise TransportError("Meritz request failed; reconcile writes before retrying") from None
        if not isinstance(data, Mapping):
            raise InvalidResponseError("Meritz response is not an object")
        if not write and path in DATA_SUCCESS_PATHS and isinstance(data.get("data"), Mapping):
            return data  # get_quote checks the returned identity and numeric price
        code = data.get("rsp_cd")
        refusal = rejection_for_code(self.broker_code, code)
        if refusal is not None:
            if refusal.kind == "token_expired":
                self._token = None
            raise refusal
        if code not in SUCCESS:
            raise InvalidResponseError("Unknown Meritz result; reconcile before retrying")
        if write:
            row = data.get("data")
            if not isinstance(row, Mapping) or row.get("warn_cls_code") is None:
                raise InvalidResponseError("Missing Meritz acknowledgement evidence")
            if row["warn_cls_code"] != "0":
                # Official docs/errors.md: any nonzero warning means NOT accepted. Never auto-confirm.
                if isinstance(row["warn_cls_code"], str) and len(row["warn_cls_code"]) == 1:
                    raise BrokerRejectedError("Meritz warning not confirmed", kind="rejected", reason="warning")
                raise InvalidResponseError("Invalid Meritz warning")
            if code != "0001":
                raise InvalidResponseError("Meritz did not acknowledge this mutation")
            _number_id(row.get("oder_no"))
        return data

    def _prefix(self):
        return "/trading/v1/orders/" if self.market == "KR" else "/trading/v1/overseas/orders/"

    def _day(self):
        return (self._order_date or datetime.now(ZoneInfo("Asia/Seoul")).date()).strftime("%Y%m%d")

    def _validate_order(self, request):
        super()._validate_order(request)
        if not self._writes_enabled:
            raise SpecUnverifiedError("Meritz writes disabled by default")
        if isinstance(request, (CancelRequest, AmendRequest)):
            if self.market == "KR" and request.venue is None:
                raise ConfigurationError("Supply the original KR venue; the ledger does not report it")
            try:
                if _number_id(request.order_id) != request.order_id:
                    raise ValueError
            except (InvalidResponseError, ValueError):
                raise ConfigurationError("Meritz order id must be a canonical positive integer") from None
        if self.market != "KR":
            if isinstance(request, (CancelRequest, AmendRequest)) and self._order_date is None:
                raise ConfigurationError("Supply the original broker order_date for US modify/cancel")
            if len(request.symbol) > 12:
                raise ConfigurationError("Meritz symbol exceeds twelve characters")
            price = getattr(request, "price", None)
            if price is not None and (price * 10000 != (price * 10000).to_integral_value()
                                      or len(format(price, "f")) > 20):
                raise ConfigurationError("Meritz US price exceeds four decimals or twenty characters")
        elif not isinstance(request, CancelRequest):
            if request.session == "pre" and (request.order_type != "market" or request.price is not None):
                raise NotSupportedError("Meritz pre-session uses the unpriced opening close code only")
            if request.session == "after" and request.order_type not in {"limit", "best", "priority"}:
                raise NotSupportedError("Meritz after-market accepts limit, best and priority only")
            if request.order_type == "close" and (request.price is not None or request.time_in_force != "day"):
                raise NotSupportedError("Meritz closing-price session is unpriced and day-only")
            if request.stop_price is not None and request.stop_price != request.stop_price.to_integral_value():
                raise ConfigurationError("Meritz KR stop price must be whole won")

    @staticmethod
    def _us_price(price):
        if price is None:
            return "0"
        text = format(price, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _body(self, request):
        if self.market != "KR":
            return {"ovrs_stck_symb_iscd": self._symbol(request.symbol, request.market),
                    "ovrs_exch_vndr_idno": MARKET_VENUES[self.market][request.venue][0],
                    "oder_cls_code": US_KINDS[request.order_type],
                    "oder_cond_cls_code": OVERSEAS_CONDITIONS[request.order_type],
                    "odqt": request.quantity, "ovrs_stck_oder_prc": self._us_price(request.price),
                    "warn_cnfr_yn": "N"}
        body = {"iscd": "A" + self._symbol(request.symbol, request.market),
                "oder_unpr": int(request.price or 0), "odqt": request.quantity,
                "oder_cls_code": "61" if request.session == "pre" else KR_KINDS[request.order_type],
                "oder_cond_cls_code": TIF[request.time_in_force], "orgl_oder_no": 0,
                "whol_rctf_cncl_yn": "N", "warn_cnfr_yn": "N",
                "exch_kind_code": KR_VENUES[request.venue or "KRX"]}
        if request.stop_price is not None:
            body["aspr_cond_prc"] = int(request.stop_price)
        return body

    def _place_order(self, request):
        row = self._response("POST", self._prefix() + request.side, body=self._body(request), write=True)["data"]
        return Order(id=_number_id(row["oder_no"]), symbol=request.symbol, side=request.side,
                     quantity=request.quantity, price=request.price, order_type=request.order_type,
                     venue=request.venue, market=self.market, status="accepted")

    def _original(self, request):
        ledger = self._ledger()
        rows = [r for r in ledger if _number_id(r["oder_no"]) == request.order_id]
        if len(rows) != 1 or self._ledger_symbol(rows[0]) != request.symbol:
            raise InvalidResponseError("Mutation requires one matching original ledger row")
        row = rows[0]
        if self.market != "KR":
            visited = set()
            while True:
                ident = _number_id(row["oder_no"])
                if ident in visited:
                    raise InvalidResponseError("Cyclic Meritz amendment chain")
                visited.add(ident)
                children = [r for r in ledger if str(r["orgl_oder_no"]) == ident
                            and r["oder_rctf_cncl_cls_code"] == "1"
                            and r["ovrs_stck_oder_stat_cls_code"] == "0"]
                if not children:
                    break
                if len(children) != 1:
                    raise InvalidResponseError("Ambiguous Meritz amendment chain")
                row = children[0]
                if self._ledger_symbol(row) != request.symbol:
                    raise InvalidResponseError("Amended order symbol differs")
            if row["oder_date"] != self._day():
                raise InvalidResponseError("Original order date differs from the configured trading date")
            suffix = MARKET_VENUES[self.market][self.venue][1]
            if suffix is None:
                current = self._response("GET", self._prefix() + "today",
                                         params={"cntg_cls_code": "0", "seln_byov_clcd": "0"})
                matches = [r for r in current.get("data", []) if r["oder_no"] == row["oder_no"]
                           and r["oder_date"] == row["oder_date"] and r["iscd"] == row["iscd"]]
                if (current.get("tr_cont") != "0" or len(matches) != 1
                        or matches[0]["ovrs_exch_vndr_idno"] != MARKET_VENUES[self.market][self.venue][0]):
                    raise InvalidResponseError("Original order venue lacks matching today evidence")
            elif not row["iscd"].endswith("." + suffix):
                raise InvalidResponseError("Original order exchange differs from the configured venue")
        return row

    def _root_number(self, row):
        # Official corrections.json: KR changes reference the FIRST order, US the latest amendment.
        if self.market == "KR" and row["rctf_cncl_cls_code"] in {"2", "3"}:
            return int(_number_id(row["orgl_oder_no"]))
        return int(_number_id(row["oder_no"]))

    def _amend_order(self, request, prepared=None):
        original = self._original(request)
        side_key = "deal_cls_code" if self.market == "KR" else "seln_byov_clcd"
        if original[side_key] != {"buy": "2", "sell": "1"}[request.side]:
            raise ConfigurationError("Amendment side differs from the original order")
        body = self._body(request)
        if self.market != "KR":
            page = self._response("GET", self._prefix() + "today",
                                  params={"cntg_cls_code": "0", "seln_byov_clcd": "0"})
            matches = [r for r in page.get("data", []) if r["oder_no"] == original["oder_no"]
                       and r["oder_date"] == original["oder_date"] and r["iscd"] == original["iscd"]]
            if (page.get("tr_cont") != "0" or len(matches) != 1
                    or matches[0]["ovrs_exch_vndr_idno"] != MARKET_VENUES[self.market][self.venue][0]
                    or matches[0]["seln_byov_clcd"] != original["seln_byov_clcd"]
                    or matches[0]["aspr_type_code"] != original["aspr_type_code"]):
                raise InvalidResponseError("Original order kind lacks matching today evidence")
            kind = US_ASPR.get(matches[0]["aspr_type_code"])
            if kind not in {"limit", "loo", "loc"}:
                raise NotSupportedError("Meritz price amendment requires an original priced order")
            body["oder_cls_code"] = US_KINDS[kind]
            body["oder_cond_cls_code"] = OVERSEAS_CONDITIONS[kind]
            body.pop("ovrs_stck_symb_iscd")
            body.pop("ovrs_exch_vndr_idno")
            body["oder_date"] = self._day()
        body["orgl_oder_no"] = self._root_number(original)
        row = self._response("POST", self._prefix() + "modify", body=body, write=True)["data"]
        return Order(id=_number_id(row["oder_no"]), symbol=request.symbol, market=self.market,
                     quantity=request.quantity, price=request.price, status="accepted")

    def _cancel_order(self, request):
        original = self._original(request)
        body = {"orgl_oder_no": self._root_number(original), "odqt": request.quantity, "warn_cnfr_yn": "N"}
        if self.market == "KR":
            body.update(iscd="A" + request.symbol, whol_rctf_cncl_yn="N",
                        exch_kind_code=KR_VENUES[request.venue or "KRX"])
        else:
            kind = US_ASPR.get(original["aspr_type_code"])
            if kind not in US_KINDS:
                raise SpecUnverifiedError("US original order kind is outside the shared contract")
            body.update(oder_date=original["oder_date"], oder_cls_code=US_KINDS[kind])
        self._response("POST", self._prefix() + "cancel", body=body, write=True)
        return Order(id=request.order_id, symbol=request.symbol, market=self.market, status="unknown")

    def _pages(self, path, params=None):
        params, seen = dict(params or {}), set()
        for _ in range(100):
            page = self._response("GET", path, params=params)
            rows = page.get("data")
            if not isinstance(rows, list) or any(not isinstance(r, Mapping) for r in rows):
                raise InvalidResponseError("Meritz list response missing")
            yield from rows
            cont = page.get("tr_cont")
            if cont == "0":
                if page["rsp_cd"] == "5762":
                    raise InvalidResponseError("Meritz pagination evidence conflicts")
                return
            key = page.get("tr_cont_key")
            if cont != "1" or not isinstance(key, str) or not key or key in seen:
                raise InvalidResponseError("Meritz continuation evidence missing or repeated")
            seen.add(key)
            params.update(tr_cont="1", tr_cont_key=key)  # significant trailing spaces preserved
        raise InvalidResponseError("Meritz pagination exceeded bounded budget")

    def _ledger(self):
        params = {"oder_date": self._day(), "deal_cls_code": "0", "cntg_cls_code": "0"}
        if self.market != "KR":
            params = {"oder_sta_date": self._day(), "oder_fin_date": self._day(), "cntg_cls_code": "0"}
        return list(self._pages(self._prefix() + "history", params))

    def _ledger_symbol(self, row):
        native = row["iscd"]
        if self.market == "KR":
            if not isinstance(native, str) or not native.startswith("A"):
                raise InvalidResponseError("Meritz native KR symbol missing prefix")
            return self._symbol(native[1:], "KR")
        root, sep, suffix = native.rpartition(".")
        known_suffixes = {v[1] for v in MARKET_VENUES[self.market].values()} - {None}
        if not sep or not suffix or (known_suffixes and suffix not in known_suffixes):
            raise InvalidResponseError("Meritz US ledger symbol has unknown exchange suffix")
        return self.canonical_symbol(root)

    def _matches_shared_venue(self, row):
        vendor = row.get("ovrs_exch_vndr_idno")
        known = {v[0] for market, venues in MARKET_VENUES.items()
                 if MARKET_CODES[market] == MARKET_CODES[self.market] for v in venues.values()}
        if not isinstance(vendor, str) or vendor not in known:
            raise InvalidResponseError("Meritz shared-market row lacks known exchange evidence")
        return vendor == MARKET_VENUES[self.market][self.venue][0]

    def list_orders(self, symbol=None):
        wanted = self._symbol(symbol, self.market) if symbol is not None else None
        def read():
            result, seen = [], set()
            ledger = self._ledger()
            today = {}
            if self.market != "KR":
                page = self._response("GET", self._prefix() + "today",
                                      params={"cntg_cls_code": "0", "seln_byov_clcd": "0"})
                if page.get("tr_cont") != "0" or not isinstance(page.get("data"), list):
                    raise InvalidResponseError("Meritz today evidence incomplete")
                for row in page["data"]:
                    key = (row["oder_date"], _number_id(row["oder_no"]))
                    if key in today:
                        raise InvalidResponseError("Meritz duplicate today identity")
                    today[key] = row
            replaced = {str(r["orgl_oder_no"]) for r in ledger
                        if self.market != "KR" and r["oder_rctf_cncl_cls_code"] in {"1", "2"}}
            kr_amended = {str(r["orgl_oder_no"]) for r in ledger
                          if self.market == "KR" and r["rctf_cncl_cls_code"] == "2"}
            kr_uncertain = {str(r["orgl_oder_no"]) for r in ledger
                            if self.market == "KR" and r["rctf_cncl_cls_code"] == "3"} & kr_amended
            for r in ledger:
                if self.market != "KR" and r["ovrs_stck_mrkt_tr_cls_code"] != MARKET_CODES[self.market]:
                    continue
                if self.market in SHARED_MARKETS:
                    evidence = today.get((r["oder_date"], _number_id(r["oder_no"])))
                    if evidence is None:
                        raise InvalidResponseError("Meritz shared-market order lacks today evidence")
                    if not self._matches_shared_venue(evidence):
                        continue
                code = self._ledger_symbol(r)
                ident = _number_id(r["oder_no"])
                if ident in seen:
                    raise InvalidResponseError("Meritz duplicate ledger identity")
                seen.add(ident)
                if wanted is not None and wanted != code:
                    continue
                if self.market == "KR":
                    q, f, remaining = (whole(r[k]) for k in ("odqt", "total_cntg_qty", "ncnt_qty"))
                    status = "rejected" if r["oder_pros_stat_cls_code"] == "3" else "unknown"
                    if status != "rejected" and r["oder_rfus_reas_code"] != "0000":
                        status = "unknown"  # no sourced rejection semantics for individual reason codes
                    elif r["oder_pros_stat_cls_code"] in {"1", "2"}:
                        if f == q and q > 0:
                            status = "filled"
                        elif remaining > 0:
                            status = "partial" if f else "open"
                        elif whole(r["cnfr_qty"]) > 0:
                            status = "cancelled"
                    price, fill = optional_price(r["oder_unpr"]), optional_price(r["cntg_unpr"])
                    side = {"1": "sell", "2": "buy"}.get(r["deal_cls_code"])
                    kind = next((k for k, v in KR_KINDS.items() if v == r["oder_cls_code"]), None)
                else:
                    q, f = whole(r["oder_dqty"]), whole(r["cntg_dqty"])
                    remaining = q - f  # official: ncnt_odqt is unreliable
                    status = {"1": "rejected", "2": "rejected", "3": "cancelled"}.get(
                        r["ovrs_stck_oder_stat_cls_code"], "unknown")
                    if r["ovrs_stck_oder_stat_cls_code"] == "0" and r["oder_rctf_cncl_cls_code"] != "2":
                        status = "filled" if f == q and q else "partial" if f else "open"
                    if r["ovrs_stck_oder_stat_cls_code"] in {"1", "2", "3"}:
                        remaining = 0
                    price, fill = optional_price(r["ovrs_stck_oder_prc"]), optional_price(r["cntg_crnc_prc"])
                    side = {"1": "sell", "2": "buy"}.get(r["seln_byov_clcd"])
                    kind = US_ASPR.get(r["aspr_type_code"])
                if f > q or remaining < 0 or f + remaining > q:
                    raise InvalidResponseError("Meritz ledger quantities inconsistent")
                if self.market == "KR":
                    if ident in kr_uncertain or str(r["orgl_oder_no"]) in kr_uncertain:
                        status, remaining = "unknown", None
                else:
                    if ident in replaced or r["oder_rctf_cncl_cls_code"] == "2":
                        status, remaining = "unknown", None
                    evidence = today.get((r["oder_date"], ident))
                    if evidence is not None:
                        if (evidence["iscd"] != r["iscd"]
                                or whole(evidence["odqt"]) != q or whole(evidence["cntg_qty"]) != f
                                or evidence["seln_byov_clcd"] != r["seln_byov_clcd"]):
                            raise InvalidResponseError("Meritz today/history identity or quantity differs")
                        cancelled, unfilled, amendable = (whole(evidence[k]) for k in
                                                         ("cncl_cnfr_qty", "ncnt_qty", "oder_able_qty"))
                        if (r["ovrs_stck_oder_stat_cls_code"] == "0" and q > f
                                and cancelled == q - f and unfilled == 0 and amendable == 0):
                            status, remaining = "cancelled", 0
                result.append(Order(id=ident, symbol=code, market=self.market, quantity=q, filled=f,
                                    remaining=remaining, status=status, price=price, side=side, order_type=kind,
                                    fill_price=fill if f == 1 else None))
            return result
        return self._call(read)

    def get_positions(self):
        def read():
            path = "/accounts/v1/me/" + ("holdings" if self.market == "KR" else "overseas/holdings")
            result = []
            for row in self._pages(path):
                if self.market == "KR":
                    if row["pros_trgt_cls_code"] != "1":
                        continue
                    # No sellable field exists in this official schema. Never assert holding == sellable.
                    if whole(row["rmnd_qty"]):
                        raise SpecUnverifiedError("Meritz KR sellable quantity is undocumented in holdings")
                    continue
                if row["ovrs_stck_mrkt_tr_cls_code"] != MARKET_CODES[self.market]:
                    continue
                if self.market in SHARED_MARKETS and not self._matches_shared_venue(row):
                    continue
                if row["crcd"] != CURRENCIES[self.market]:
                    raise InvalidResponseError("Meritz holding currency inconsistent")
                q, sell = decimal(row["rmnd_dqty"]), decimal(row["seln_able_dqty"])
                result.append(Position(symbol=self.canonical_symbol(row["ovrs_stck_symb_iscd"]),
                                       name=row["isnm"], market=self.market, quantity=int(q), sellable=int(sell),
                                       quantity_exact=q, sellable_exact=sell, average_price=decimal(row["avrg_unpr"]),
                                       last=decimal(row["psnt_prc"]), market_value=decimal(row["frcr_vltn_amt"])))
            return result
        return self._call(read)

    def available_cash(self):
        def read():
            if self.market == "KR":
                return decimal(self._response("GET", "/accounts/v1/me/deposit")["data"]["oder_able_cash_amt"])
            data = self._response("GET", self._prefix() + "estimate", params={"intg_marg_iqry_yn": "N"})
            if data.get("tr_cont") != "0":
                raise InvalidResponseError("Meritz funding continuation is unsupported")
            rows = [r for r in data["data"] if r["crcd"] == CURRENCIES[self.market]]
            if len(rows) != 1:
                raise InvalidResponseError("Meritz funding requires one matching currency row")
            return decimal(rows[0]["oder_crnc_oder_able_camt"])
        return self._call(read)

    def get_balance(self):
        def read():
            cash = self.available_cash()
            if self.market == "KR":
                rows = self._pages("/accounts/v1/me/holdings")
                value = sum((decimal(r["vltn_amt"]) for r in rows if r["pros_trgt_cls_code"] == "1"), Decimal(0))
            else:
                value = sum((p.market_value for p in self.get_positions()), Decimal(0))
            return Balance(currency=CURRENCIES[self.market], cash_available=cash, position_value=value)
        return self._call(read)

    def get_quote(self, symbol, *, market="KR"):
        symbol = self._symbol(symbol, market)
        def read():
            path, params = "/market/v1/prices", {"mrkt_div_code": "J", "iscd": symbol}
            if self.market != "KR":
                if MARKET_VENUES[self.market][self.venue][1] is None:
                    raise SpecUnverifiedError("Quote market code is not documented for this venue")
                path, params = "/market/v1/overseas/prices", {"mrkt_div_code": "OV", "iscd": symbol,
                                "mrkt_cls_code": MARKET_VENUES[self.market][self.venue][1],
                                "dely_rltm_cls_code": "1" if self.market in {"HK", "CN_SH"} else "0"}
            row = self._response("GET", path, params=params)["data"]
            key, price = ("shrn_iscd", "stck_prpr") if self.market == "KR" else ("iscd", "prpr")
            if row[key] != symbol:
                raise InvalidResponseError("Meritz quote identity inconsistent")
            return Quote(symbol=self.canonical_symbol(symbol), market=self.market, last=decimal(row[price]))
        return self._call(read)
