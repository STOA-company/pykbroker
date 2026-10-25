"""One guarded mutation path for SDK and MCP. No retries or execution engine."""

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import TypeVar
from zoneinfo import ZoneInfo

from pykbroker.exceptions import BrokerError, CircuitOpenError, DuplicateOrderError, RiskCapError, SafetyError
from pykbroker.models import AmendRequest, CancelRequest, OrderRequest
from pykbroker.models.order import PRICED_TYPES

from .dry_run import DryRun, mask_account
from .idempotency import IdempotencyStore, scope_key
from .kill_switch import KillSwitch
from .risk_caps import RiskLedger, RiskLimits

T = TypeVar("T")
logger = logging.getLogger(__name__)


class OrderSafety:
    def __init__(
        self,
        *,
        dry_run: bool = True,
        limits: RiskLimits | None = None,
        risk_ledger: RiskLedger | None = None,
        idempotency: IdempotencyStore | None = None,
        kill_switch: KillSwitch | None = None,
        breaker_allows: Callable[[], bool] | None = None,
        valuation: Callable[[OrderRequest], Decimal] | None = None,
        market_order_allowed: Callable[[OrderRequest], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.dry_run = DryRun(dry_run)
        self.limits, self.risk_ledger, self.idempotency = limits, risk_ledger, idempotency
        self.kill_switch = kill_switch if kill_switch is not None else KillSwitch()
        self.breaker_allows = breaker_allows
        self.valuation, self.market_order_allowed = valuation, market_order_allowed
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Seoul")))

    def execute(self, broker: str, account: str, request: OrderRequest | CancelRequest, submit: Callable[[], T]) -> T:
        try:
            return self._execute(broker, account, request, submit)
        except BrokerError:
            raise
        except Exception:
            raise SafetyError("Safety provider failed; operation blocked, reconcile before retrying") from None

    def _execute(self, broker: str, account: str, request: OrderRequest | CancelRequest, submit: Callable[[], T]) -> T:
        # The first three stages are invariant across submission and cancellation.
        self.dry_run.require_live()
        if self.limits is None or self.risk_ledger is None:
            raise RiskCapError("risk_caps: limits and usage ledger are required")
        DryRun(self.limits.dry_run).require_live()
        amount = Decimal(0)
        action = (
            "cancel"
            if isinstance(request, CancelRequest)
            else "amend"
            if isinstance(request, AmendRequest)
            else "place"
        )
        if isinstance(request, OrderRequest):
            if (
                request.order_type in ("market", "close", "best", "priority", "midpoint", "moo", "moc", "vwap", "twap")
                or request.session != "regular"
            ):
                if self.market_order_allowed is None or self.market_order_allowed(request) is not True:
                    raise RiskCapError("risk_caps: market-order session authorization required")
            if request.market == "KR" and request.price is not None and request.order_type in PRICED_TYPES:
                amount = request.price * request.quantity
            elif self.valuation is None:
                raise RiskCapError("risk_caps: a conservative KRW valuation is required")
            if self.valuation is not None:
                estimate = self.valuation(request)
                if not isinstance(estimate, Decimal) or not estimate.is_finite() or estimate <= 0:
                    raise RiskCapError("risk_caps: invalid KRW valuation")
                amount = max(amount, estimate)
        scope = scope_key(broker, account)
        now = self.clock()
        if now.tzinfo is None:
            raise SafetyError("Safety clock must include a timezone")
        day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
        self.risk_ledger.reserve(scope, day, amount, self.limits)
        if self.idempotency is None:
            raise SafetyError("An idempotency store is required")
        key = hashlib.sha256(request.client_key.encode()).hexdigest()
        if self.idempotency.claim(scope + ":" + action, key) is not True:
            raise DuplicateOrderError("duplicate: client key already claimed; reconcile status")
        self.kill_switch.require_open(broker, account)
        if self.breaker_allows is not None and self.breaker_allows() is not True:
            raise CircuitOpenError("circuit_breaker: operation blocked")
        logger.info("broker=%s account=%s action=%s", broker, mask_account(account), action)
        # A failure retains both the amount reservation and the client-key claim.
        return submit()
