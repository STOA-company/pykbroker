"""KIS translation over the optional Pykis client and domestic/overseas helpers."""

import inspect
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from pykbroker.exceptions import ConfigurationError, FractionalNotSupportedError, InvalidResponseError, MissingDependencyError, NotSupportedError
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import equity_rules
from pykbroker.safety.gate import OrderSafety

from ._common import BaseBroker, PageRows, check_response, decimal, field, optional_price, whole

# KIS 국내주식 현재가 시세 output — two independent halt signals, both on the price row
# (_pykis/scope/stock/api/response/price.py:4 iscd_stat_cls_code, :16 temp_stop_yn).
# 58 is 거래정지 and 00 is 그외(정상). 51 관리·52 위험·53 경고 and friends are neither:
# they are restrictions we have not verified, so they read as «unknown», never as «trading».
KR_HALT_CODES = {"58"}
KR_TRADING_CODES = {"00"}
YES_NO = {"Y": True, "N": False}

VENUES = {"NASDAQ": ("NAS", "NASD"), "NYSE": ("NYS", "NYSE"), "AMEX": ("AMS", "AMEX")}

KST = ZoneInfo("Asia/Seoul")
# A mock account cannot read open orders the way a real one does: the vendor's
# `revisable_order` refuses it outright (_pykis/scope/account/api/revisable_order.py:16),
# and `get_open_orders_domestic` is only a call into it (base/domestic.py:114).
# The daily ledger is the one order TR the vendor does switch to a mock id for
# (VTTC8001R, daily_order.py:71), so the mock path reads that instead -- and says
# through `realtime_open_orders` that it is a ledger, not a live snapshot.
UNFILLED = "미체결"
# base/overseas.py:377 hardcodes TTTS3018R, and the official overseas_stock/inquire_nccs example
# (open-trading-api, inquire_nccs.py:92) also has `tr_id = "TTTS3018R"` with no mock branch: the
# vendor documents no mock open-order TR for US. What the official docs do serve on the mock domain
# is the order ledger (overseas_stock/inquire_ccnl, inquire_ccnl.py:127-131: TTTS3035R real,
# VTTS3035R mock; mock accepts only the all-rows filters, inquire_ccnl.py:57-63) and the mock
# cancel (order_rvsecncl.py:102-106: VTTT1004U, which Pykis already routes). So a US mock reads
# the ledger and keeps the unfilled rows -- a ledger, not a live snapshot (realtime_open_orders False).
US_MOCK_LEDGER_TR = "VTTS3035R"
US_MOCK_LEDGER_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
US_TZ = ZoneInfo("America/New_York")


# Pykis retries inside its transport: `_execute_request` is wrapped by tenacity (3 attempts on
# everything but ReadTimeout and KisTransientError) and `request` loops again on KisTransientError
# (_pykis/client/client.py:208-214, 245-280). An order POST that fails there can reach the venue
# up to three times with nothing above the transport able to see it. Writes go through the raw,
# unwrapped `_execute_request` instead; reads keep the vendor's retries.
_SINGLE_ATTEMPT = threading.local()
_ARM_LOCK = threading.Lock()
_TENACITY_MARKS = ("retry", "retry_with")  # what tenacity's `@retry` (and any `functools.wraps` copy of it) carries


def _bypass_target(execute: Any) -> Callable[..., Any]:
    """The call under every wrapper, or a refusal when a retry layer could survive the unwrap.

    Only the outermost layer may carry tenacity's marks (it is the one being bypassed). A mark on
    any layer below it or on the unwrapped call -- a second `@retry`, or a decorator stacked over
    `@retry` -- means the chain is not the shape we know, so the write is refused, not retried.
    """
    layers: list[Any] = []

    def visit(layer: Any) -> bool:
        layers.append(layer)
        return False

    try:
        raw = inspect.unwrap(execute, stop=visit)
    except ValueError:  # a wrapper cycle
        raw = execute
    below = [*layers[1:], raw]
    if raw is execute or not callable(raw) or any(hasattr(layer, mark) for layer in below for mark in _TENACITY_MARKS):
        raise ConfigurationError("KIS transport retries cannot be bypassed; refusing to send an order")
    return raw


def _arm_single_attempt(vendor: Any) -> None:
    """Route `vendor.client.request` to one attempt while a write on this thread asks for it."""
    transport = getattr(vendor, "client", None)
    execute = getattr(type(transport), "_execute_request", None)
    if execute is None:
        return  # not the Pykis transport: nothing hidden retries under this client
    raw = _bypass_target(execute)
    with _ARM_LOCK:
        if getattr(transport, "_pykbroker_single_attempt", False):
            return
        retrying = transport.request

        def request(*args: Any, **kwargs: Any) -> Any:
            if getattr(_SINGLE_ATTEMPT, "active", False):
                return raw(transport, *args, **kwargs)
            return retrying(*args, **kwargs)

        transport.request = request
        transport._pykbroker_single_attempt = True


def _yes_no(value: Any) -> bool | None:
    """The vendor models temp_stop_yn as a bool; a raw envelope still carries Y/N."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return YES_NO.get(value.strip().upper())
    return None


def _status_state(value: Any) -> bool | None:
    text = str(value).strip() if value is not None else ""
    if text in KR_HALT_CODES:
        return True
    return False if text in KR_TRADING_CODES else None


def _ledger_status(cancelled: Any, filled: int, remaining: int) -> str:
    """A ledger row states quantities; nothing here is inferred from an absent row."""
    if cancelled is True or (isinstance(cancelled, str) and cancelled.strip().upper() == "Y"):
        return "cancelled"
    if remaining == 0:
        return "filled" if filled > 0 else "unknown"
    return "partial" if filled > 0 else "open"


# Official continuation: page count is unstated, so a list that does not end within this many pages is refused.
KIS_MAX_PAGES = 20
# CTRP6504R output1 row identity: product, exchange, product type, currency, loan date.
KIS_US_ROW_KEY = ("pdno", "ovrs_excg_cd", "prdt_type_cd", "buy_crcy_cd", "loan_dt")


def _kis_more(flag: Any) -> bool:
    """KIS response header tr_cont: F/M = more pages, D/E = last page. Anything else is unreadable."""
    if flag is None:
        return False
    if not isinstance(flag, str):
        raise InvalidResponseError("KIS continuation flag is invalid")
    flag = flag.strip()
    if flag in ("F", "M"):
        return True
    if flag in ("", "D", "E"):
        return False
    raise InvalidResponseError("KIS continuation flag is invalid")


class KIS(BaseBroker):
    broker_code = "kis"
    supports_virtual = True

    def __init__(
        self,
        *,
        account: str,
        client: Any = None,
        api: Any = None,
        client_factory: Callable[..., Any] | None = None,
        client_options: Mapping[str, Any] | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
        venue: str | None = None,
        cash_provider: Callable[[Any], Decimal] | None = None,
        virtual: bool = False,
        is_integrated: bool = False,
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        if market == "US" and venue not in VENUES:
            raise ConfigurationError("US KIS requires NASDAQ, NYSE, or AMEX venue")
        if not isinstance(is_integrated, bool):
            raise ConfigurationError("is_integrated must be a boolean")
        self.is_integrated = is_integrated
        self.venue = venue
        self._cash_provider = cash_provider
        self._client, self._api = client, api
        self._factory, self._options = client_factory, dict(client_options or {})
        # The mode is a contract argument; vendor options must not carry a second answer.
        if "virtual_account" in self._options:
            raise ConfigurationError("Set the account mode with virtual=, not client_options")
        self._scope = None
        self._owns_client = client is None
        # Consume once in US rules; the query's configured venue and ticker bind the fact.
        self._us_native_orderability: dict[tuple[str, str], bool | None] = {}

    def _connect(self) -> tuple[Any, Any]:
        try:
            if self._client is None:
                factory = self._factory or import_module("Pykis._pykis").PyKis
                if not self._options and self._factory is None:
                    raise ConfigurationError("Supply KIS client_options or an authenticated client")
                options = {
                    **self._options,
                    "virtual_account": self.virtual,
                    "realtime": False,
                    "market_auto_sync": False,
                    "late_init": True,
                }
                self._client = factory(**options)
            self._verify_mode(self._client)
            if self._scope is None:
                self._scope = self._client.account(self._account)
            if self._api is None:
                self._api = import_module("Pykis.base.domestic" if self.market == "KR" else "Pykis.base.overseas")
        except ImportError:
            raise MissingDependencyError("Supply Pykis source and pykbroker[kis], or inject client and api") from None
        return self._scope, self._api

    def _verify_mode(self, client: Any) -> None:
        """An undisclosed or disagreeing mode refuses the call; it is never assumed."""
        mode = getattr(getattr(client, "key", None), "virtual_account", None)
        if not isinstance(mode, bool):
            raise ConfigurationError("KIS client does not disclose its account mode; refusing")
        if mode is not self.virtual:
            raise ConfigurationError("KIS client account mode disagrees with the requested mode")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            # KIS overseas has no market order; the executor must plan a limit price itself.
            supports_market_order=self.market == "KR",
            supports_single_order_cancel=True,
            supports_virtual_account=True,
            virtual=self.virtual,
            # Mock accounts have no live open-order TR: KR and US read the (delayed) order ledger.
            supports_order_history=True,
            realtime_open_orders=not self.virtual,
            requires_venue=self.market == "US",
            venues=tuple(VENUES) if self.market == "US" else ("KRX",),
            markets=(self.market,),
        )

    def get_rules(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        trading_day: Callable[[date], bool] | None = None,
    ) -> Rules:
        """KR rules come from the live price row: tick, the daily band, and the halt flag."""
        if self.market != "KR":
            code = self._symbol(symbol, "US")
            key = (self.venue, code)
            rules = super().get_rules(code, now=now, trading_day=trading_day)
            if rules.halted is True:
                self._us_native_orderability.clear()
                return rules  # An affirmative caller halt is never talked away.
            if key not in self._us_native_orderability:
                def read_native() -> bool | None:
                    scope, api = self._connect()
                    output = check_response(api.get_price_overseas_detail(
                        kis_account=scope, excd=VENUES[self.venue][0], code=code), "kis")["output"]
                    return self._us_orderability(output)
                self._us_native_orderability[key] = self._call(read_native)
            orderable = self._us_native_orderability.pop(key)
            self._us_native_orderability.clear()
            # US uses the native broker-orderability guard, not an invented exchange halt enum.
            # A positive two-sided quote/caller False cannot replace a missing native answer.
            return rules.model_copy(update={"halted": False if orderable is True else None})
        code = self._symbol(symbol, "KR")

        def read() -> Rules:
            scope, api = self._connect()
            output = check_response(api.get_price_domestic(kis_account=scope, stock_code=code), "kis")["output"]
            halted = self._halt_state(output, code)
            return equity_rules(
                code,
                price=decimal(field(output, "stck_prpr")),
                prev_close=optional_price(field(output, "stck_sdpr")),
                # 상한가/하한가 sit on this very row (price.py:47,49). They are the venue's
                # answer for this symbol today; the ±30% rule is only the fallback below.
                limit_up=optional_price(field(output, "stck_mxpr")),
                limit_down=optional_price(field(output, "stck_llam")),
                halted=halted,
                now=now,
                trading_day=trading_day,
            )

        return self._call(read)

    def _halt_state(self, output: Any, code: str) -> bool | None:
        """Combine the price row's two halt signals with any halt notice the caller holds.

        An affirmed halt wins from whichever side it comes: the venue never talks a notice
        away, and a notice never talks the venue's halt away. Anything less than an
        affirmative «trading» on every signal we can read stays unknown.
        """
        # A field the vendor did not send is not a signal; a field it sent always is,
        # even when its value is one we cannot read.
        temporary, status = field(output, "temp_stop_yn"), field(output, "iscd_stat_cls_code")
        signals = [_yes_no(temporary)] if temporary is not None else []
        signals += [_status_state(status)] if status is not None else []
        if True in signals:
            venue = True
        elif not signals or None in signals:
            venue = None
        else:
            venue = False
        notice = self._halt_from_provider(code)
        if venue is True or notice is True:
            return True
        return venue if venue is False else notice

    @staticmethod
    def _us_orderability(output: Any) -> bool | None:
        # Official HHDFS76200200 e_ordyn example is this exact semantic literal.
        # Other/missing values have no published exhaustive enum and remain unknown.
        value = field(output, "e_ordyn")
        return True if type(value) is str and value == "매매 가능" else None

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        symbol = self._symbol(symbol, market)

        def read() -> Quote:
            scope, api = self._connect()
            if market == "KR":
                price = check_response(api.get_price_domestic(kis_account=scope, stock_code=symbol), "kis")["output"]
                book = check_response(api.get_quote_domestic(kis_account=scope, stock_code=symbol), "kis")["output1"]
                return Quote(
                    symbol=symbol,
                    last=decimal(price["stck_prpr"]),
                    bid=optional_price(book["bidp1"]),
                    ask=optional_price(book["askp1"]),
                    prev_close=optional_price(price.get("stck_sdpr")),
                )
            kwargs = dict(kis_account=scope, excd=VENUES[self.venue][0], code=symbol)
            price = check_response(api.get_price_overseas_detail(**kwargs), "kis")["output"]
            book = check_response(api.get_quote_overseas(**kwargs), "kis")["output2"]
            quote = Quote(
                symbol=symbol,
                market="US",
                last=decimal(price["last"]),
                bid=optional_price(book["pbid1"]),
                ask=optional_price(book["pask1"]),
            )
            self._us_native_orderability[(self.venue, symbol)] = self._us_orderability(price)
            return quote

        return self._call(read)

    def get_balance(self) -> Balance:
        def read() -> Balance:
            scope, _ = self._connect()
            if self.market == "US":
                if self._cash_provider is None and self.virtual:
                    raise ConfigurationError("US mock cash requires a caller-supplied cash_provider")
                return Balance(
                    currency="USD",
                    cash_available=(decimal(self._cash_provider(scope))
                                    if self._cash_provider else self._us_native_cash(scope)),
                    position_value=sum((p.market_value for p in self.get_positions()), Decimal(0)),
                )
            balance, amount = scope.balance_all(), scope.amount(code=None)
            return Balance(
                cash_available=decimal(field(amount, "ord_psbl_cash")),
                position_value=decimal(field(balance, "evlu_amt_smtl_amt")),
                orderable_cash=decimal(field(amount, "ord_psbl_cash")),
            )

        return self._call(read)

    def available_cash(self) -> Decimal:
        if self.market == "US" or self.is_integrated:
            return self._call(lambda: self._us_cash(self._connect()[0]))
        return super().available_cash()

    def _us_native_cash(self, scope: Any) -> Decimal:
        _, api = self._connect()
        data = check_response(api.get_balances(kis_account=scope, NATN_CD="840", TR_MKET_CD="00"), "kis")
        rows = field(data, "output2")
        if not isinstance(rows, list):
            raise InvalidResponseError("KIS native cash block missing")
        usd = [row for row in rows if field(row, "crcy_cd") == "USD"]
        if len(usd) != 1:
            raise InvalidResponseError("KIS native cash requires one USD row")
        return min(decimal(field(usd[0], "frcr_dncl_amt_2")), decimal(field(usd[0], "frcr_drwg_psbl_amt_1")))

    def _us_cash(self, scope: Any) -> Decimal:
        # Official domestic_stock/intgr_margin (TTTC0869R): native cash100 buying power.
        # usd_itgr_ord_psbl_amt is broker-computed unified buying power, never total assets/FX.
        # Keep normal accounts on usd_gnrl_ord_psbl_amt. Mode comes from existing account policy.
        currency_basis = "02" if self.market == "KR" else "01"
        response = scope.client.request(
            "get", "/uapi/domestic-stock/v1/trading/intgr-margin",
            headers={"tr_id": "TTTC0869R"},
            params=scope.account.build_body({
                "CMA_EVLU_AMT_ICLD_YN": "N", "WCRC_FRCR_DVSN_CD": currency_basis,
                "FWEX_CTRT_FRCR_DVSN_CD": currency_basis,
            }), response=None,
        )
        # Pykis response=None returns requests.Response, not its usual normalized helper envelope.
        if not isinstance(response, Mapping):
            if field(response, "status_code") != 200:
                raise InvalidResponseError("KIS buying-power HTTP request failed")
            response = response.json()
        envelope = check_response(response, "kis")
        if str(field(envelope, "rt_cd")) != "0":
            raise InvalidResponseError("KIS buying-power response lacks a success status")
        output = field(envelope, "output")
        key = ("stck_itgr_cash100_ord_psbl_amt" if self.market == "KR"
               else "usd_itgr_ord_psbl_amt" if self.is_integrated else "usd_gnrl_ord_psbl_amt")
        return decimal(field(output, key))

    def _us_holding_rows(self, scope: Any, api: Any) -> list[Any]:
        """Every page of CTRP6504R output1 (official overseas_stock/inquire_present_balance example).

        Response header tr_cont F/M means more rows follow: the same request is repeated with the
        request header tr_cont=N. D/E (or no flag) ends the list. A list that does not end within
        KIS_MAX_PAGES is refused, never cut short: a missing later page would read a held symbol as 0.
        A row repeated on a later page counts once; a conflicting repeat is refused (PageRows).
        """
        raw = api.get_balances(kis_account=scope, NATN_CD="840", TR_MKET_CD="00")
        rows = PageRows(KIS_US_ROW_KEY, "KIS US")
        for page in range(1, KIS_MAX_PAGES + 1):
            block = check_response(raw, "kis")["output1"]
            if not isinstance(block, list):
                raise InvalidResponseError("KIS US holdings block missing")
            rows.add(block)
            if not _kis_more(field(raw, "tr_cont")):
                return rows.rows
            if page < KIS_MAX_PAGES:
                raw = self._us_present_balance_next(scope)
        raise InvalidResponseError("KIS US holdings exceeded the page limit; refusing a truncated result")

    @staticmethod
    def _us_present_balance_next(scope: Any) -> dict[str, Any]:
        # Same TR and parameters as Pykis.base.overseas.get_balances, plus the continuation header.
        response = scope.client.request(
            "get", "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            headers={"tr_id": "CTRP6504R", "tr_cont": "N"},
            params=scope.account.build_body({
                "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "01",
            }), response=None,
        )
        if field(response, "status_code") != 200:
            raise InvalidResponseError("KIS holdings continuation request failed")
        headers = field(response, "headers") or {}
        return {"json_data": response.json(), "tr_cont": headers.get("tr_cont")}

    def get_positions(self) -> list[Position]:
        def read() -> list[Position]:
            scope, api = self._connect()
            if self.market == "US":
                rows = self._us_holding_rows(scope, api)
                return [
                    Position(
                        symbol=row["pdno"],
                        name=row["prdt_name"],
                        market="US",
                        quantity=whole(row["ccld_qty_smtl1"]),
                        sellable=whole(row["ord_psbl_qty1"]),
                        average_price=decimal(row["avg_unpr3"]),
                        last=decimal(row["ovrs_now_pric1"]),
                        market_value=decimal(row["frcr_evlu_amt2"]),
                    )
                    for row in rows
                ]
            return [
                Position(
                    symbol=field(row, "pdno"),
                    name=field(row, "prdt_name", ""),
                    quantity=whole(field(row, "hldg_qty")),
                    sellable=whole(field(row, "ord_psbl_qty")),
                    average_price=decimal(field(row, "pchs_avg_pric")),
                    last=decimal(field(row, "prpr")),
                    market_value=decimal(field(row, "evlu_amt")),
                )
                for row in field(scope.balance_all(), "stocks")
            ]

        return self._call(read)

    def list_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is not None:
            symbol = self._symbol(symbol, self.market)
        if self.market == "KR" and self.virtual:
            return self._call(lambda: self._ledger_orders(symbol))
        if self.market == "US" and self.virtual:
            return self._call(lambda: self._us_mock_open_orders(symbol))

        def read() -> list[Order]:
            scope, api = self._connect()
            if self.market == "US":
                response = api.get_open_orders_overseas_all(kis_account=scope, market=VENUES[self.venue][1])
                rows = check_response(response, "kis")["output"]
                return [
                    Order(id=str(row["odno"]), symbol=row["pdno"], market="US", status="unknown", virtual=self.virtual)
                    for row in rows
                    if symbol is None or row["pdno"] == symbol
                ]
            rows = field(api.get_open_orders_domestic(kis_account=scope), "orders")
            result = []
            for row in rows:
                code = field(row, "pdno")
                if symbol is not None and symbol != code:
                    continue
                order_id = field(row, "odno")
                if order_id is None:
                    raise InvalidResponseError("KIS order identifier missing")
                remaining = whole(field(row, "psbl_qty"))
                result.append(
                    Order(
                        id=str(order_id),
                        symbol=code,
                        remaining=remaining,
                        status="open" if remaining > 0 else "unknown",
                        virtual=self.virtual,
                    )
                )
            return result

        return self._call(read)

    def _ledger_orders(self, symbol: str | None) -> list[Order]:
        """Unfilled rows of today's daily ledger — the mock domain's only order TR."""
        scope, _ = self._connect()
        today = datetime.now(KST)
        response = scope.daily_order(start_date=today, end_date=today, code=symbol or "", ccld=UNFILLED)
        result = []
        for row in field(response, "orders", ()):
            code = field(row, "pdno")
            if symbol is not None and code != symbol:
                continue
            order_id = field(row, "odno")
            if order_id is None or not str(order_id).strip():
                raise InvalidResponseError("KIS order identifier missing")
            filled, remaining = whole(field(row, "tot_ccld_qty")), whole(field(row, "rmn_qty"))
            result.append(
                Order(
                    id=str(order_id),
                    symbol=code,
                    quantity=whole(field(row, "ord_qty")),
                    filled=filled,
                    remaining=remaining,
                    status=_ledger_status(field(row, "cncl_yn"), filled, remaining),
                    virtual=True,
                )
            )
        return result

    def _us_mock_ledger_rows(self, scope: Any) -> list[Any]:
        """Every page of today's (US local date) mock order ledger, VTTS3035R, all-rows filters only."""
        today = datetime.now(US_TZ).strftime("%Y%m%d")
        rows = PageRows(("odno", "rvse_cncl_dvsn", "ord_dt", "ord_tmd", "orgn_odno"), "KIS US mock ledger")
        cont: dict[str, str] = {"CTX_AREA_NK200": "", "CTX_AREA_FK200": ""}
        for page in range(1, KIS_MAX_PAGES + 1):
            response = scope.client.request(
                "get", US_MOCK_LEDGER_PATH,
                headers={"tr_id": US_MOCK_LEDGER_TR, "tr_cont": "" if page == 1 else "N"},
                params=scope.account.build_body({
                    "PDNO": "", "ORD_STRT_DT": today, "ORD_END_DT": today, "SLL_BUY_DVSN": "00",
                    "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "", "SORT_SQN": "DS", "ORD_DT": "",
                    "ORD_GNO_BRNO": "", "ODNO": "", **cont,
                }), response=None,
            )
            if not isinstance(response, Mapping):
                if field(response, "status_code") != 200:
                    raise InvalidResponseError("KIS US mock ledger HTTP request failed")
                headers = field(response, "headers") or {}
                body = {"json_data": response.json(), "tr_cont": headers.get("tr_cont")}
            else:
                body = response
            envelope = check_response(body, "kis")
            if str(field(envelope, "rt_cd")) != "0":
                raise InvalidResponseError("KIS US mock ledger response lacks a success status")
            block = field(envelope, "output")
            if not isinstance(block, list):
                raise InvalidResponseError("KIS US mock ledger block missing")
            rows.add(block)
            if not _kis_more(field(body, "tr_cont")):
                return rows.rows
            cont = {"CTX_AREA_NK200": str(field(envelope, "ctx_area_nk200", "") or ""),
                    "CTX_AREA_FK200": str(field(envelope, "ctx_area_fk200", "") or "")}
        raise InvalidResponseError("KIS US mock ledger exceeded the page limit; refusing a truncated result")

    @staticmethod
    def _us_open_rows(rows: list[Any]) -> list[Any]:
        """Ledger rows still open: remaining > 0, not a cancel row, not named by a cancel row."""
        cancelled = {str(field(row, "orgn_odno")) for row in rows
                     if field(row, "rvse_cncl_dvsn") == "02" and field(row, "orgn_odno")}
        return [row for row in rows
                if field(row, "rvse_cncl_dvsn") != "02" and str(field(row, "odno")) not in cancelled
                and whole(field(row, "nccs_qty")) > 0]

    def _us_mock_open_orders(self, symbol: str | None) -> list[Order]:
        scope, _ = self._connect()
        result = []
        for row in self._us_open_rows(self._us_mock_ledger_rows(scope)):
            code = field(row, "pdno")
            if symbol is not None and code != symbol:
                continue
            order_id = field(row, "odno")
            if order_id is None or not str(order_id).strip():
                raise InvalidResponseError("KIS order identifier missing")
            quantity, filled = whole(field(row, "ft_ord_qty")), whole(field(row, "ft_ccld_qty"))
            remaining = whole(field(row, "nccs_qty"))
            if filled + remaining > quantity:
                raise InvalidResponseError("KIS ledger quantities exceed the order")
            result.append(Order(id=str(order_id), symbol=code, market="US", quantity=quantity, filled=filled,
                                remaining=remaining, status=_ledger_status(False, filled, remaining),
                                virtual=True))
        return result

    def status(self, order: Order) -> Order:
        """Read the broker ledger, including filled orders absent from the open-order snapshot."""
        symbol = self._symbol(order.symbol, order.market)
        if order.market != self.market:
            raise ConfigurationError("Order market disagrees with the adapter")

        def read() -> Order:
            scope, api = self._connect()
            if self.market == "KR":
                today = datetime.now(KST)
                rows = field(scope.daily_order_all(start_date=today, end_date=today, code=symbol), "orders")
            elif self.virtual:
                rows = self._us_mock_ledger_rows(scope)
            else:
                rows = check_response(api.get_orders_overseas_all(kis_account=scope, code=symbol), "kis")["output"]
            # A US mock cancel is its own ledger row naming the original in orgn_odno.
            named_cancelled = {str(field(r, "orgn_odno")) for r in rows
                               if field(r, "rvse_cncl_dvsn") == "02" and field(r, "orgn_odno")} \
                if self.market == "US" else set()
            for row in rows:
                if str(field(row, "odno")) != order.id or field(row, "pdno") != symbol:
                    continue
                kr = self.market == "KR"
                quantity = whole(field(row, "ord_qty" if kr else "ft_ord_qty"))
                filled = whole(field(row, "tot_ccld_qty" if kr else "ft_ccld_qty"))
                remaining = whole(field(row, "rmn_qty" if kr else "nccs_qty"))
                if filled + remaining > quantity:
                    raise InvalidResponseError("KIS ledger quantities exceed the order")
                cancelled = field(row, "cncl_yn") if kr else (
                    field(row, "rvse_cncl_dvsn") == "02" or str(field(row, "odno")) in named_cancelled)
                state = _ledger_status(cancelled, filled, remaining)
                # Partial fills plus zero remaining can be a cancellation/rejection: no invented full fill.
                if state == "filled" and filled != quantity:
                    state = "unknown"
                average = (optional_price(field(row, "avg_prvs")) if kr and filled else
                           optional_price(field(row, "ft_ccld_unpr3")) if not kr and filled == 1 else None)
                return Order.model_validate({**order.model_dump(), "quantity": quantity, "filled": filled,
                    "remaining": remaining, "fill_price": average, "status": state})
            return order.model_copy(update={"status": "unknown"})

        return self._call(read)

    def _cancel_order(self, request: CancelRequest) -> Order:
        """Resolve one ID and symbol, then cancel precisely that order and requested quantity once."""
        scope, api = self._connect()
        symbol = self._symbol(request.symbol, request.market)
        if self.market == "KR":
            if self.virtual:
                today = datetime.now(KST)
                rows = field(scope.daily_order_all(start_date=today, end_date=today,
                    code=symbol, ccld=UNFILLED), "orders")
            else:
                rows = field(api.get_open_orders_domestic(kis_account=scope), "orders")
        elif self.virtual:
            rows = self._us_open_rows(self._us_mock_ledger_rows(scope))
        else:
            rows = check_response(api.get_open_orders_overseas_all(
                kis_account=scope, market=VENUES[self.venue][1]), "kis")["output"]
        matches = [row for row in rows if str(field(row, "odno")) == request.order_id
                   and field(row, "pdno") == symbol]
        if len(matches) != 1:
            raise NotSupportedError("KIS exact open order not found; reconcile before cancellation")
        row = matches[0]
        remaining = whole(field(row, "rmn_qty" if self.virtual else "psbl_qty") if self.market == "KR"
                          else field(row, "nccs_qty"))
        if request.quantity > remaining:
            raise ConfigurationError("Cancellation quantity exceeds the broker remaining quantity")
        branch = field(row, "krx_fwdg_ord_orgno", field(row, "ord_gno_brno"))
        if not isinstance(branch, str) or not branch.strip():
            raise InvalidResponseError("KIS cancellation branch identifier missing")
        ref = SimpleNamespace(krx_fwdg_ord_orgno=branch, odno=request.order_id)
        with self._single_attempt():
            if self.market == "KR":
                response = scope.cancel(order=ref, qty=request.quantity)
            else:
                response = scope.overseas_cancel(market=VENUES[self.venue][1], code=symbol,
                                                order=ref, qty=request.quantity)
        envelope = check_response(response, "kis")
        if isinstance(response, Mapping) and str(field(envelope, "rt_cd")) != "0":
            raise InvalidResponseError("KIS cancellation response lacks a success status")
        output = field(envelope, "output", envelope)
        cancel_id = field(output, "ODNO", field(output, "odno"))
        if cancel_id is None or not str(cancel_id).strip():
            raise InvalidResponseError("KIS cancellation response lacks an order identifier")
        # Accepted cancellation is not proof the original order has been cancelled.
        return Order(id=request.order_id, symbol=symbol, market=request.market,
                     quantity=request.quantity, status="accepted", virtual=self.virtual)

    @contextmanager
    def _single_attempt(self):
        """A write that fails is unknown, not retryable: the vendor transport sends it once."""
        _arm_single_attempt(self._client)
        previous = getattr(_SINGLE_ATTEMPT, "active", False)
        _SINGLE_ATTEMPT.active = True
        try:
            yield
        finally:
            _SINGLE_ATTEMPT.active = previous

    def _place_order(self, request: OrderRequest) -> Order:
        if request.fractional:
            # KIS has no fractional-share order TR (overseas_stock order(): ORD_QTY whole). Refuse; never int().
            raise FractionalNotSupportedError("KIS orders are whole shares")
        scope, api = self._connect()
        symbol = self._symbol(request.symbol, request.market)
        if self.market == "KR":
            with self._single_attempt():
                response = api.post_order_domestic(
                    kis_account=scope,
                    stock_code=symbol,
                    order_type=request.side,
                    order_qty=request.quantity,
                    order_price=int(request.price) if request.order_type == "limit" else 0,
                    dvsn={"limit": "지정가", "market": "시장가", "close": "장후 시간외"}[request.order_type],
                )
        else:
            if request.order_type != "limit":
                raise NotSupportedError("US KIS only implements limit orders")
            if request.venue not in (None, self.venue):
                raise NotSupportedError("Order venue must match the configured US venue")
            with self._single_attempt():
                response = api.post_order_overseas(
                    kis_account=scope,
                    ticker=symbol,
                    market=VENUES[self.venue][1],
                    order_type=request.side,
                    order_qty=request.quantity,
                    order_price=float(request.price),
                )
        envelope = check_response(response, "kis")
        if isinstance(response, Mapping) and field(envelope, "rt_cd") is None:
            raise InvalidResponseError("KIS submission response lacks a success status")
        output = field(envelope, "output", envelope)
        order_id = field(output, "ODNO", field(output, "odno"))
        if order_id is None or not str(order_id).strip():
            raise InvalidResponseError("KIS response lacks an order identifier; reconcile before retrying")
        return Order(
            id=str(order_id),
            symbol=symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status="accepted",
            market=request.market,
            virtual=self.virtual,
        )

    def close(self) -> None:
        if not self._closed and self._owns_client and self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if callable(close):
                    self._call(close)
            finally:
                super().close()
        else:
            super().close()
