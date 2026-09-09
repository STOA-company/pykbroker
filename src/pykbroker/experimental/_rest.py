"""Injected REST boundary. No default transport, credentials, or request budgets."""

from collections.abc import Mapping
from typing import Any, Protocol

from pykbroker.brokers._common import BaseBroker
from pykbroker.exceptions import ConfigurationError, NotSupportedError
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.rate_limit import RateLimiter


class RestClient(Protocol):
    def post(self, path: str, *, headers: dict[str, str], json: dict[str, Any]) -> dict[str, Any]: ...


class RestBroker(BaseBroker):
    def __init__(
        self,
        *,
        account: str,
        client: RestClient | None = None,
        codes: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
        safety: OrderSafety | None = None,
        market: str = "KR",
    ) -> None:
        super().__init__(account=account, safety=safety, market=market)
        if market != "KR":
            raise NotSupportedError("This experimental REST mapping is domestic only")
        self._client, self._codes = client, dict(codes or {})
        self._headers, self._limiter = dict(headers or {}), rate_limiter

    def _code(self, name: str) -> str:
        value = self._codes.get(name)
        if not isinstance(value, str) or not value:
            raise ConfigurationError("Unverified vendor code; supply a reviewed codes mapping")
        return value

    def _request(self, path: str, tr: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is None or self._limiter is None:
            raise ConfigurationError("Supply a REST client and TR rate limiter")
        self._limiter.wait(tr)
        selector = "tr_cd" if self.broker_code == "ls" else "api-id"
        headers = {**self._headers, "content-type": "application/json", selector: tr}
        if self.broker_code == "ls":
            headers.update(tr_cont="N", tr_cont_key="")
        return self._client.post(path, headers=headers, json=body)
