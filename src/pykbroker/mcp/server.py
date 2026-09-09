"""FastMCP tools follow the pycex decorator/JSON/stdio convention."""

import json
import logging
import os
import threading
import tomllib
from collections.abc import Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import wraps
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt, ValidationError

from pykbroker.brokers._common import BaseBroker
from pykbroker.exceptions import BrokerError, ConfigurationError, SafetyError
from pykbroker.factory import create_broker
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.risk_caps import RiskLimits
from pykbroker.safety.storage import SQLiteLedger

logger = logging.getLogger(__name__)


def load_limits(path: Path) -> RiskLimits | None:
    """Invalid/missing authorization hides mutation tools; omitted dry_run stays true."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        limits = RiskLimits.model_validate(data)
        return limits if limits.allow_orders is True else None
    except (OSError, ValueError, ValidationError):
        return None


def safe_tool(function):
    """Do not let validation or caller exceptions copy tool arguments into logs."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BrokerError:
            raise
        except Exception:
            raise ConfigurationError("Invalid tool input or provider response; operation stopped") from None

    return wrapped


def create_server(
    *,
    limits_path: Path | None = None,
    broker_factory: Callable[[OrderSafety], BaseBroker] | None = None,
    safety: OrderSafety | None = None,
) -> FastMCP:
    path = limits_path if limits_path is not None else Path.home() / ".pykbroker" / "limits.toml"
    limits = load_limits(path)
    broker: BaseBroker | None = None
    lock = threading.RLock()

    def still_authorized(broker_name: str, account: str) -> bool:
        # File deletion, revocation, or any policy edit blocks until restart.
        return limits is None or load_limits(path) != limits

    ledger = SQLiteLedger(path.parent / "orders.sqlite3")
    guard = (
        safety
        if safety is not None
        else OrderSafety(
            dry_run=limits.dry_run if limits is not None else True,
            limits=limits,
            risk_ledger=ledger,
            idempotency=ledger,
            kill_switch=KillSwitch(still_authorized),
        )
    )

    def get_broker() -> BaseBroker:
        nonlocal broker
        if broker is None:
            candidate = (
                broker_factory(guard)
                if broker_factory is not None
                else create_broker(os.environ.get("PYKBROKER_BROKER", "kis"), safety=guard)
            )
            if not isinstance(candidate, BaseBroker) or candidate.safety is not guard:
                raise ConfigurationError("MCP factory must return a BaseBroker bound to the supplied safety gate")
            broker = candidate
        return broker

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield {}
        finally:
            with lock:
                if broker is not None:
                    broker.close()

    app = FastMCP(
        "Korean Stock Brokers",
        instructions=(
            "국내 증권사의 시세·잔고·보유 종목·주문 상태·규칙을 조회합니다. "
            "주문 도구는 limits.toml의 명시적 설정이 있을 때만 등록됩니다."
        ),
        lifespan=lifespan,
    )

    @app.tool()
    @safe_tool
    def get_quote(symbol: str) -> str:
        """현재가와 제공 가능한 매수·매도 1호가를 조회합니다."""
        with lock:
            return get_broker().get_quote(symbol).model_dump_json()

    @app.tool()
    @safe_tool
    def get_balance() -> str:
        """주문 가능 현금과 보유 종목 평가액을 조회합니다."""
        with lock:
            return get_broker().get_balance().model_dump_json()

    @app.tool()
    @safe_tool
    def get_positions() -> str:
        """보유 종목별 수량과 가격을 조회합니다."""
        with lock:
            return json.dumps([p.model_dump(mode="json") for p in get_broker().get_positions()], ensure_ascii=False)

    @app.tool()
    @safe_tool
    def list_orders(symbol: str | None = None) -> str:
        """제공 가능한 주문 목록을 조회합니다. 목록에서 사라진 주문의 체결을 추정하지 않습니다."""
        with lock:
            return json.dumps([o.model_dump(mode="json") for o in get_broker().list_orders(symbol)], ensure_ascii=False)

    @app.tool()
    @safe_tool
    def get_rules(symbol: str) -> str:
        """기본 시장 규칙을 조회합니다. 알 수 없는 호가 단위는 null입니다."""
        with lock:
            return get_broker().get_rules(symbol).model_dump_json()

    if limits is not None:

        def authorize() -> None:
            if load_limits(path) != limits:
                raise SafetyError("Order settings changed or were removed; restart with reviewed settings")
            # An injected guard may be stricter, but may not bypass file dry-run/caps.
            if guard.limits != limits or guard.dry_run.enabled is not limits.dry_run:
                raise ConfigurationError("MCP safety must match the file's limits and dry_run")
            guard.dry_run.require_live()

        @app.tool()
        @safe_tool
        def place_order(
            symbol: str,
            side: str,
            quantity: StrictInt,
            client_key: str,
            price: str | None = None,
            order_type: str = "limit",
        ) -> str:
            """주문합니다. 기본 드라이런은 전송을 차단하며 client_key는 재시도에도 유지해야 합니다."""
            with lock:
                authorize()
                request = OrderRequest(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    client_key=client_key,
                    price=Decimal(price) if price is not None else None,
                    order_type=order_type,
                )
                # BaseBroker is the sole SDK/MCP dry-run -> caps -> key -> vendor boundary.
                return get_broker().place_order(request).model_dump_json()

        @app.tool()
        @safe_tool
        def cancel_order(order_id: str, symbol: str, quantity: StrictInt, client_key: str) -> str:
            """한 주문의 미체결 수량만 취소합니다. 종목 전체 취소로 확대하지 않습니다."""
            with lock:
                authorize()
                request = CancelRequest(order_id=order_id, symbol=symbol, quantity=quantity, client_key=client_key)
                return get_broker().cancel_order(request).model_dump_json()

    return app


# As in pycex, importing exposes a configured FastMCP object; no broker is constructed.
mcp = create_server()


def run_server() -> None:
    mcp.run(transport="stdio")
