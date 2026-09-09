"""Caller-supplied KRW limits; no default amounts and no account schema."""

import threading
from datetime import date
from decimal import Decimal
from typing import Protocol

from pydantic import StrictBool

from pykbroker.exceptions import RiskCapError
from pykbroker.models._base import Model, PositiveMoney


class RiskLimits(Model):
    per_order_krw_cap: PositiveMoney
    daily_krw_cap: PositiveMoney
    allow_orders: StrictBool = False
    dry_run: StrictBool = True


class RiskLedger(Protocol):
    def reserve(self, scope: str, day: date, amount: Decimal, limits: RiskLimits) -> None:
        """Atomically check caps and debit daily allowance, or raise RiskCapError.

        Cancellations use zero; rejected/duplicate/uncertain requests keep their
        reservation. Reservations are gross submitted KRW, never netted by sells.
        """
        ...


def check_caps(amount: Decimal, spent: Decimal, limits: RiskLimits | None) -> None:
    if limits is None or limits.allow_orders is not True:
        raise RiskCapError("risk_caps: limits unconfigured or orders disabled")
    if not isinstance(amount, Decimal) or not isinstance(spent, Decimal):
        raise RiskCapError("risk_caps: amounts must be Decimal")
    if not amount.is_finite() or not spent.is_finite() or amount < 0 or spent < 0:
        raise RiskCapError("risk_caps: invalid amount or usage")
    if amount > limits.per_order_krw_cap:
        raise RiskCapError("risk_caps: per-order cap exceeded")
    if spent + amount > limits.daily_krw_cap:
        raise RiskCapError("risk_caps: daily cap exceeded")


class MemoryRiskLedger:
    """Explicit process-local demo store. A new process loses usage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: dict[tuple[str, date], Decimal] = {}

    def reserve(self, scope: str, day: date, amount: Decimal, limits: RiskLimits) -> None:
        with self._lock:
            key = (scope, day)
            spent = self._usage.get(key, Decimal(0))
            check_caps(amount, spent, limits)
            self._usage[key] = spent + amount
