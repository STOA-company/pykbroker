"""Kiwoom domestic read half over a stored token. Orders, cancels and token issuance are not here."""

import json
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from pykbroker.exceptions import (
    BrokerRejectedError,
    ConfigurationError,
    InvalidResponseError,
    NotSupportedError,
    SpecUnverifiedError,
)
from pykbroker.models import Balance, CancelRequest, Capabilities, Order, OrderRequest, Position, Quote, Rules
from pykbroker.rules.kr import equity_rules
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.rate_limit import RateLimiter, credential_rate_limiter

from ._common import BaseBroker, decimal, whole

# Copied from trader quantus_backend/traders/kiwoom_order.py (KIWOOM_API_BASE_URL, KIWOOM_TR_RATE_LIMITS);
# the trader marks the limits unverified (account TRs 1, market data 5). The mock host is spelled here
# once so that `virtual` alone picks it.
KIWOOM_API_BASE_URL = "https://api.kiwoom.com"
KIWOOM_MOCK_BASE_URL = "https://mockapi.kiwoom.com"
KIWOOM_TR_RATE_LIMITS: dict[str, int] = {"kt00018": 1, "ka10001": 5}
# A stored token is the only credential this half holds; nothing else may ride in client_options.
CLIENT_OPTION_KEYS = {"token", "token_type", "app_key"}


class _KiwoomRestClient:
    """Copied from trader kiwoom_order.py: a narrow JSON POST boundary with no extra dependency."""

    def __init__(self, base_url: str = KIWOOM_API_BASE_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    def post(self, path: str, *, headers: dict, json: dict) -> dict:
        request = urllib.request.Request(f"{self.base_url}{path}", data=_dumps(json), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return _loads(response.read())


def _dumps(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _loads(value: bytes) -> dict:
    decoded = json.loads(value.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Kiwoom response must be a JSON object")
    return decoded


class Kiwoom(BaseBroker):
    broker_code = "kiwoom"
    supports_virtual = True

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
        halt_provider: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(account=account, safety=safety, market=market, virtual=virtual, halt_provider=halt_provider)
        if market != "KR":
            raise NotSupportedError("Kiwoom US is not implemented")
        options = dict(client_options)
        if not set(options) <= CLIENT_OPTION_KEYS:
            raise ConfigurationError("client_options accepts only token, token_type and app_key")
        token, app_key = options.get("token"), options.get("app_key")
        if not (isinstance(token, str) and token.strip() and isinstance(app_key, str) and app_key.strip()):
            raise ConfigurationError("Supply a stored Kiwoom token and app_key; issuing one is not implemented")
        token_type = options.get("token_type", "Bearer")
        self._authorization = token if token.lower().startswith(f"{token_type.lower()} ") else f"{token_type} {token}"
        self._codes = dict(codes or {})
        self._limiter = rate_limiter or credential_rate_limiter("kiwoom", app_key, KIWOOM_TR_RATE_LIMITS)
        # The host follows `virtual` alone; the factory cannot choose it.
        host = KIWOOM_MOCK_BASE_URL if virtual else KIWOOM_API_BASE_URL
        self._client = (client_factory or _KiwoomRestClient)(base_url=host)

    def _code(self, name: str) -> str:
        value = self._codes.get(name)
        if not isinstance(value, str) or not value:
            raise ConfigurationError("Unverified vendor code; supply a reviewed codes mapping")
        return value

    def _response(self, path: str, tr: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": self._authorization,
            "api-id": tr,
        }
        self._limiter.wait(tr)
        response = self._client.post(path, headers=headers, json=body)
        if not isinstance(response, Mapping):
            raise InvalidResponseError("Kiwoom response must be a JSON object")
        # None/0/"0" is the acceptance set of experimental/kiwoom.py; other codes are not classified further.
        if response.get("return_code") not in (None, 0, "0"):
            raise BrokerRejectedError("Kiwoom rejected the request")
        return response

    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_spot=True,
            supports_limit_order=False,
            supports_virtual_account=True,
            virtual=self.virtual,
            venues=("KRX",),
            markets=("KR",),
        )

    def get_quote(self, symbol: str, *, market: str = "KR") -> Quote:
        code = self._symbol(symbol, market)

        # Bid/ask (ka10004) and the venue's band are not read: no evidence for their fields yet.
        def read() -> Quote:
            row = self._response("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})
            return Quote(symbol=code, last=decimal(row["cur_prc"]))

        return self._call(read)

    def get_rules(
        self, symbol: str, *, now: datetime | None = None, trading_day: Callable[[date], bool] | None = None
    ) -> Rules:
        code = self._symbol(symbol, "KR")
        return equity_rules(
            code, self.get_quote(code).last, halted=self._halt_from_provider(code), now=now, trading_day=trading_day
        )

    def get_balance(self) -> Balance:
        raise SpecUnverifiedError("Order-available cash (kt00001) is unverified; kt00018 holds no such field")

    def get_positions(self) -> list[Position]:
        def read() -> list[Position]:
            body = {"dmst_stex_tp": self._code("exchange"), "qry_tp": self._code("query_type")}
            rows = self._response("/api/dostk/acnt", "kt00018", body)["acnt_evlt_remn_indv_tot"]
            return [
                Position(
                    symbol=self._symbol(row["stk_cd"], "KR"),
                    name=row["stk_nm"],
                    quantity=whole(row["rmnd_qty"]),
                    sellable=whole(row["trde_able_qty"]),
                    average_price=decimal(row["pur_pric"]),
                    last=decimal(row["cur_prc"]),
                    market_value=decimal(row["evlt_amt"]),
                )
                for row in rows
            ]

        return self._call(read)

    def place_order(self, request: OrderRequest) -> Order:
        raise NotSupportedError("Kiwoom orders are not implemented in the read half")

    def cancel_order(self, request: CancelRequest) -> Order:
        raise NotSupportedError("Kiwoom cancellation is not implemented in the read half")
