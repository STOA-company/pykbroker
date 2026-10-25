"""Toss OpenAPI 1.2.24 contract mock for ``POST /api/v1/orders`` (no network).

Stands in for the SDK bridge (``Toss(sdk_factory=...)``): it validates the order the broker would
send against the vendor schema in ``tests/fixtures/toss_order_create_openapi_1_2_24.json`` (a verbatim
subset of https://openapi.tossinvest.com/openapi-docs/latest/openapi.json) and answers the way the vendor
documents it — ``400 invalid-request`` for shape errors, ``422 *-outside-regular-hours`` outside the US
regular session window, ``200 {orderId}`` otherwise. 4xx answers surface as ``TossRejectedError(code)``,
exactly what ``_TossBridge._raise_write_error`` produces from a real 4xx.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pykbroker.exceptions import TossRejectedError

_FIXTURE = Path(__file__).parent / "fixtures" / "toss_order_create_openapi_1_2_24.json"
_ET = ZoneInfo("America/New_York")


def load_contract() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _schema_by_title(contract: dict, title: str) -> dict:
    for option in contract["OrderCreateRequest"]["oneOf"]:
        if option["title"] == title:
            return option
    raise KeyError(title)


def _is_us_symbol(symbol: str) -> bool:
    # KRX symbols are 6 characters (digits or alnum); US tickers are letters (dots allowed).
    return not re.fullmatch(r"[0-9A-Za-z]{6}", symbol) or symbol.isalpha()


def _scale(text: str) -> int:
    return -Decimal(text).normalize().as_tuple().exponent if "." in text else 0


class ContractRejection(TossRejectedError):
    """A documented vendor 4xx; keeps the HTTP status so a transport double can replay it."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


class ContractMockTossSdk:
    """Fake SDK bridge whose ``submit_order`` enforces the vendor contract."""

    def __init__(self, *, clock=None, **options):
        self.options = options
        self.calls: list = []
        self.accepted: list = []
        self.contract = load_contract()
        self._clock = clock or (lambda: datetime.now(_ET))
        self._seq = 0

    # --- reads used by the broker around an order ----------------------------
    async def get_prices(self, symbols):
        return [{"symbol": symbols[0], "currency": "USD", "lastPrice": "190.00"}]

    async def get_orderbook(self, symbol):
        return {"currency": "USD", "asks": [{"price": "190.01"}], "bids": [{"price": "189.99"}]}

    async def get_positions(self):
        return {"marketValue": {"amount": {"usd": "0", "krw": "0"}}, "items": []}

    async def list_open_orders(self, symbol=None):
        return {"hasNext": False, "orders": []}

    async def aclose(self):
        return None

    # --- the contract --------------------------------------------------------
    def _payload(self, kwargs: dict) -> dict:
        """Mirror ``toss_openapi.models.order.OrderCreateRequest.to_payload`` (None fields dropped)."""
        mapping = {
            "symbol": "symbol", "side": "side", "order_type": "orderType", "quantity": "quantity",
            "price": "price", "time_in_force": "timeInForce", "client_order_id": "clientOrderId",
            "order_amount": "orderAmount",
        }
        unknown = set(kwargs) - set(mapping)
        assert not unknown, f"SDK bridge received unknown fields: {sorted(unknown)}"
        return {mapping[k]: v for k, v in kwargs.items() if v is not None}

    def _documented(self, status: int, code: str, example: str) -> None:
        documented = self.contract["responses"][str(status)]["examples"]
        assert example in documented, (status, example)
        assert documented[example]["value"]["error"]["code"] == code

    def _reject(self, status: int, code: str, example: str) -> None:
        self._documented(status, code, example)
        raise ContractRejection(status, code)

    def _in_regular_window(self) -> bool:
        now = self._clock().astimezone(_ET)
        if now.weekday() >= 5:
            return False
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        end = now.replace(hour=15, minute=0, second=0, microsecond=0)
        return start <= now < end

    def validate(self, payload: dict) -> str:
        """Return the matched oneOf title or raise the documented 4xx."""
        has_qty, has_amount = "quantity" in payload, "orderAmount" in payload
        if has_qty == has_amount:
            self._reject(400, "invalid-request", "quantityOrAmountRequired")
        title = "OrderCreateAmountBased" if has_amount else "OrderCreateQuantityBased"
        schema = _schema_by_title(self.contract, title)
        for name in schema["required"]:
            assert name in payload, f"{title} requires {name}"
        for name, value in payload.items():
            spec = schema["properties"].get(name)
            if spec is None:
                # e.g. price/timeInForce on an amount order: not a property of that shape.
                self._reject(400, "invalid-request", "amountUsMarketOnly")
            if spec["type"] == "string":
                assert isinstance(value, str), (name, value)
                if "enum" in spec and value not in spec["enum"]:
                    self._reject(400, "invalid-request", "invalidOrderType" if name == "orderType" else "invalidSide")
                if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                    self._reject(400, "invalid-request", "invalidQuantityFormat")
                if "maxLength" in spec and len(value) > spec["maxLength"]:
                    self._reject(400, "invalid-request", "invalidQuantityFormat")
        if has_amount:
            if payload["orderType"] != "MARKET" or not _is_us_symbol(payload["symbol"]):
                self._reject(400, "invalid-request", "amountUsMarketOnly")
            if not self._in_regular_window():
                self._reject(422, "amount-order-outside-regular-hours", "amountOrderOutsideRegularHours")
            return title
        if payload["orderType"] == "MARKET" and "price" in payload:
            self._reject(400, "invalid-request", "limitPriceRequired")
        if payload["orderType"] == "LIMIT" and "price" not in payload:
            self._reject(400, "invalid-request", "limitPriceRequired")
        if _scale(payload["quantity"]) > 0:
            us_market_sell = payload["orderType"] == "MARKET" and payload["side"] == "SELL"
            if not (us_market_sell and _is_us_symbol(payload["symbol"])):
                self._reject(400, "invalid-request", "fractionalQuantityUsSellOnly")
            if _scale(payload["quantity"]) > 6:
                self._reject(400, "invalid-request", "fractionalQuantityScaleExceeded")
            if not self._in_regular_window():
                self._reject(422, "fractional-quantity-outside-regular-hours",
                             "fractionalQuantityOutsideRegularHours")
        return title

    async def submit_order(self, **kwargs):
        payload = self._payload(kwargs)
        self.calls.append(("submit_order", payload))
        self.validate(payload)
        self._seq += 1
        order_id = f"contract-order-{self._seq}"
        self.accepted.append({**payload, "orderId": order_id})
        return {"submitted": True, "order_id": order_id}


class ContractTransport:
    """Stands in for ``toss_openapi.transport.http.HttpTransport`` under the REAL ``_TossBridge`` and SDK models.

    The JSON body the vendor SDK builds (``OrderCreateRequest.to_payload``) is validated against the vendor
    schema; documented 4xx answers come back as ``toss_openapi.errors.TossApiError`` exactly as the HTTP
    transport raises them. ``expired_tokens`` makes the server answer ``401 expired-token``. No network.
    """

    def __init__(self, *, clock=None, expired_tokens=()):
        self.server = ContractMockTossSdk(clock=clock)
        self.calls: list = []
        self.expired_tokens = set(expired_tokens)

    async def request(self, method, path, *, json_body=None, token=None, account_seq=None, retryable=True, **_):
        from toss_openapi.errors import TossApiError

        self.calls.append((method, path, dict(json_body or {}), retryable, account_seq))
        if token in self.expired_tokens:
            self.server._documented(401, "expired-token", "expiredToken")
            raise TossApiError("vendor detail", status_code=401, code="expired-token")
        assert (method, path) == ("POST", "/api/v1/orders"), (method, path)
        try:
            self.server.validate(dict(json_body or {}))
        except ContractRejection as exc:
            raise TossApiError("vendor detail", status_code=exc.status, code=exc.code) from None
        self.server._seq += 1
        return {"orderId": f"contract-order-{self.server._seq}"}
