"""IBKR option chain / quote / history reader (Client Portal Web API). Read only: no orders, no auth code.

Response shapes come from ``tests/fixtures/ibkr/openapi-options-subset.json`` (IB REST API 2.41.0). The caller
passes an already-authenticated session (headers / cookies); this module never signs or logs in.
Not wired into ``brokers/__init__`` or MCP yet. IB may need ``/iserver/accounts`` called once per session
before snapshots (OpenAPI snapshot description) — that call is the caller's, outside this piece.
"""

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pykbroker.exceptions import BrokerError, InvalidResponseError, MissingDependencyError, TransportError
from pykbroker.models.option import Bar, OptionContract, OptionQuote

# Snapshot field numbers. Source: OpenAPI "mdFields" description sentences (each line "`<n>` - <Name>. ...").
FIELDS: dict[str, str] = {
    "last": "31",  # "Last Price" (prefix C = previous close, H = halted)
    "bid": "84",  # "Bid Price"
    "ask": "86",  # "Ask Price"
    "ask_size": "85",  # "Ask Size"
    "bid_size": "88",  # "Bid Size"
    "volume": "87",  # "Volume. Volume for the day"
    "iv_pct": "7633",  # "Implied Vol. %. The implied volatility for the specific strike of the option"
    "delta": "7308",  # "Delta"
    "open_interest": "7638",  # "Option Open Interest"
}
DEFAULT_FIELDS = ("last", "bid", "ask", "iv_pct", "delta")
_AVAILABILITY = "6509"  # "Market Data Availability"; first char N = Not Subscribed
_META_KEYS = {"conid", "conidEx", "_updated", "6119", "server_id", _AVAILABILITY, "topic"}
_MAX_CONIDS, _MAX_FIELDS = 100, 50  # OpenAPI snapshot description / mdFields description
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class IbkrOptionError(BrokerError):
    """Base: the read could not be completed; the subclass says what was missing."""


class IbkrNotAuthenticated(IbkrOptionError):
    """HTTP 401 — the session passed in is not (or no longer) authenticated."""


class IbkrRequestError(IbkrOptionError):
    """Other HTTP 4xx; ``status`` and IB's ``error`` text (OpenAPI ErrorResponse) are kept."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"IB rejected the request (HTTP {status}): {detail}")
        self.status = status


class UnderlyingNotFound(IbkrOptionError):
    """secdef/search found no instrument with that symbol."""


class NoOptionSection(IbkrOptionError):
    """The underlying exists but IB lists no OPT section (no listed options)."""


class NoStrikes(IbkrOptionError):
    """secdef/strikes returned no strikes (month not listed, or search not called first)."""


class ContractNotFound(IbkrOptionError):
    """secdef/info has no contract for that month/strike/right."""


class ExpiredContract(IbkrOptionError):
    """The contract is past expiry; IB gives no history for expired options (PLAN-C fact 3)."""


class MarketDataNotSubscribed(IbkrOptionError):
    """Field 6509 starts with N: the account lacks the market data subscription."""

    def __init__(self, conids: list[int]) -> None:
        super().__init__(f"no market data subscription for conid(s) {conids}")
        self.conids = conids


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _parse_month(month: str) -> date | None:
    """«JAN24» -> first day of that month; None if not that shape."""
    m = month.strip().upper()
    if len(m) != 5 or m[:3] not in _MONTHS or not m[3:].isdigit():
        return None
    return date(2000 + int(m[3:]), _MONTHS.index(m[:3]) + 1, 1)


class IbkrOptions:
    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        verify: bool | str = True,
        timeout: float = 10.0,
        transport: Any = None,
        snapshot_retries: int = 3,
        snapshot_wait: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise MissingDependencyError("pip install 'pykbroker[ibkr]'") from exc
        self._httpx = httpx
        self._http = httpx.Client(
            base_url=base_url, headers=dict(headers or {}), cookies=dict(cookies or {}),
            verify=verify, timeout=timeout, transport=transport,
        )  # fmt: skip
        self._retries, self._wait, self._sleep, self._today = snapshot_retries, snapshot_wait, sleep, today
        self._months: dict[int, list[str]] = {}
        self._contracts: dict[int, OptionContract] = {}

    def close(self) -> None:
        self._http.close()

    # -- transport ---------------------------------------------------------------------------------------
    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        try:
            resp = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except self._httpx.HTTPError as exc:
            raise TransportError(f"IB request failed: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            raise IbkrNotAuthenticated("IB session is not authenticated")
        if resp.status_code >= 500:
            raise TransportError(f"IB server error (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError as exc:
            raise InvalidResponseError("IB response is not JSON") from exc
        if resp.status_code >= 400:
            detail = body.get("error") if isinstance(body, dict) else None
            raise IbkrRequestError(resp.status_code, str(detail or "no detail"))
        return body

    # -- chain -------------------------------------------------------------------------------------------
    def find_underlying(self, symbol: str) -> int:
        """Underlying conid for ``symbol`` that has an OPT section; remembers its months for expiry_months."""
        body = self._get("/iserver/secdef/search", {"symbol": symbol, "secType": "STK"})
        if not isinstance(body, list):
            raise InvalidResponseError("secdef/search: expected a list")
        found = [r for r in body if isinstance(r, dict) and str(r.get("symbol") or "").upper() == symbol.upper()]
        if not found:
            raise UnderlyingNotFound(f"IB has no instrument with symbol {symbol!r}")
        for row in found:
            for sec in row.get("sections") or []:
                if sec.get("secType") == "OPT":
                    try:
                        conid = int(row["conid"])
                    except (KeyError, ValueError) as exc:
                        raise InvalidResponseError("secdef/search: bad conid") from exc
                    self._months[conid] = [m for m in str(sec.get("months") or "").split(";") if m]
                    return conid
        raise NoOptionSection(f"{symbol!r} has no listed options at IB")

    def expiry_months(self, conid: int) -> list[str]:
        if conid not in self._months:
            raise IbkrOptionError("call find_underlying first (IB strikes/info need the search call before)")
        return list(self._months[conid])

    def strikes(self, conid: int, month: str, exchange: str = "SMART") -> dict[str, list[Decimal]]:
        self._refuse_past_month(month)
        body = self._get("/iserver/secdef/strikes", {"conid": conid, "sectype": "OPT", "month": month, "exchange": exchange})
        if not isinstance(body, dict):
            raise InvalidResponseError("secdef/strikes: expected an object")
        out = {side: [d for d in (_dec(x) for x in body.get(side) or []) if d is not None] for side in ("call", "put")}
        if not out["call"] and not out["put"]:
            raise NoStrikes(f"IB lists no strikes for conid {conid} month {month}")
        return out

    def option_contracts(self, conid: int, month: str, strike: Decimal | float | str, right: str) -> list[OptionContract]:
        right = right.upper()
        if right not in ("C", "P"):
            raise ValueError("right must be 'C' or 'P'")
        self._refuse_past_month(month)
        body = self._get(
            "/iserver/secdef/info",
            {"conid": conid, "sectype": "OPT", "month": month, "strike": str(strike), "right": right},
        )  # fmt: skip
        rows = body if isinstance(body, list) else [body]
        contracts = []
        for row in rows:
            if not isinstance(row, dict) or row.get("right") != right or "conid" not in row:
                continue
            raw_date = row.get("maturityDate")
            try:
                maturity = datetime.strptime(str(raw_date), "%Y%m%d").date()
            except ValueError:
                maturity = None
            c = OptionContract(
                conid=int(row["conid"]), symbol=str(row.get("ticker") or ""), right=right,
                strike=_dec(row.get("strike")) or Decimal(str(strike)), maturity=maturity,
                maturity_raw=None if raw_date is None else str(raw_date),
                exchange=row.get("exchange"), currency=row.get("currency"),
            )  # fmt: skip
            self._contracts[c.conid] = c
            contracts.append(c)
        if not contracts:
            raise ContractNotFound(f"no {right} contract at strike {strike} for conid {conid} month {month}")
        if all(c.maturity is not None and c.maturity < self._today() for c in contracts):
            raise ExpiredContract(f"contract expired {contracts[0].maturity}")
        return contracts

    def _refuse_past_month(self, month: str) -> None:
        first = _parse_month(month)
        today = self._today()
        if first and (first.year, first.month) < (today.year, today.month):
            raise ExpiredContract(f"month {month} is already past; IB lists no expired options")

    # -- quotes ------------------------------------------------------------------------------------------
    def snapshot(self, conids: Iterable[int], fields: Iterable[str] = DEFAULT_FIELDS) -> list[OptionQuote]:
        """Quotes in ``conids`` order. Empty first answers are re-asked ``snapshot_retries`` times, then returned
        with ``has_data=False`` and ``None`` fields. Raises MarketDataNotSubscribed if every row says N."""
        ids = list(dict.fromkeys(int(c) for c in conids))
        nums = list(dict.fromkeys(FIELDS.get(f, f) for f in fields))
        if not ids or not nums or len(ids) > _MAX_CONIDS or len(nums) > _MAX_FIELDS:
            raise ValueError(f"need 1..{_MAX_CONIDS} conids and 1..{_MAX_FIELDS} fields")
        # 6509 is always asked so "not subscribed" can be told apart from "still starting".
        ask = ",".join([*nums, _AVAILABILITY] if _AVAILABILITY not in nums else nums)
        rows: dict[int, dict[str, Any]] = {}
        pending = ids
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(self._wait)
            body = self._get("/iserver/marketdata/snapshot", {"conids": ",".join(map(str, pending)), "fields": ask})
            if not isinstance(body, list):
                raise InvalidResponseError("marketdata/snapshot: expected a list")
            for row in body:
                if isinstance(row, dict) and "conid" in row:
                    rows[int(row["conid"])] = {**rows.get(int(row["conid"]), {}), **row}
            pending = [c for c in ids if not any(k in rows.get(c, {}) for k in nums if k != _AVAILABILITY)]
            if not pending:
                break
        quotes = [self._quote(c, rows.get(c, {})) for c in ids]
        if all((q.availability or "")[:1] == "N" and not q.has_data for q in quotes):
            raise MarketDataNotSubscribed(ids)
        return quotes

    @staticmethod
    def _quote(conid: int, row: dict[str, Any]) -> OptionQuote:
        raw = {k: str(v) for k, v in row.items() if k not in _META_KEYS}
        data: dict[str, Any] = {}
        unverified: list[str] = []
        for name, num in FIELDS.items():
            if num not in raw:
                continue
            text = raw[num].strip()
            if name == "last":  # prefix rule is in the OpenAPI field list; anything else stays unparsed
                data["last_is_prev_close"], data["halted"] = text[:1] == "C", text[:1] == "H"
                text = text[1:] if text[:1] in ("C", "H") else text
            if text == "":
                continue
            value = _dec(text)
            if value is None:  # e.g. "1.2K": no OpenAPI basis for suffixes -> keep raw, flag it
                unverified.append(num)
            else:
                data[name] = value
        return OptionQuote(
            conid=conid, availability=row.get(_AVAILABILITY), has_data=bool(raw),
            unverified=tuple(unverified), raw=raw, **data,
        )  # fmt: skip

    def history(
        self, conid: int, period: str, bar: str, *, source: str = "Last", outside_rth: bool = False
    ) -> list[Bar]:
        known = self._contracts.get(conid)
        if known and known.maturity and known.maturity < self._today():
            raise ExpiredContract(f"conid {conid} expired {known.maturity}; IB gives no history for it")
        body = self._get(
            "/iserver/marketdata/history",
            {"conid": conid, "period": period, "bar": bar, "source": source, "outsideRth": str(outside_rth).lower()},
        )  # fmt: skip
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise InvalidResponseError("marketdata/history: no data array")
        bars = []
        for b in body["data"]:
            try:
                bars.append(Bar(
                    time=datetime.fromtimestamp(int(b["t"]) / 1000, UTC),
                    open=Decimal(str(b["o"])), high=Decimal(str(b["h"])), low=Decimal(str(b["l"])),
                    close=Decimal(str(b["c"])), volume=_dec(b.get("v")),
                ))  # fmt: skip
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise InvalidResponseError("marketdata/history: bad bar") from exc
        return bars

    def unsubscribe_all(self) -> bool:
        body = self._get("/iserver/marketdata/unsubscribeall", {})
        if not isinstance(body, dict) or "unsubscribed" not in body:
            raise InvalidResponseError("unsubscribeall: no 'unsubscribed' key")
        return bool(body["unsubscribed"])
