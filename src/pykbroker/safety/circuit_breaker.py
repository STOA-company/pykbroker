"""Pure threshold evaluation with caller-supplied metrics and thresholds."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def evaluate_breaker(
    metrics: Mapping[str, Any] | None = None, thresholds: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    try:

        def number(source: Mapping[str, Any] | None, key: str) -> Decimal:
            if source is None or isinstance(source[key], bool):
                raise ValueError
            value = Decimal(str(source[key]))
            if not value.is_finite():
                raise ValueError
            return value

        pnl = number(metrics, "realized_pnl_pct")
        rate = number(metrics, "error_rate")
        count = number(metrics, "order_count")
        consecutive = number(metrics, "consecutive_errors")
        loss_cap = number(thresholds, "max_loss_pct")
        rate_cap = number(thresholds, "max_error_rate")
        count_cap = number(thresholds, "max_order_count")
        consecutive_cap = number(thresholds, "max_consecutive_errors")
        if not 0 <= rate <= 1 or not 0 < rate_cap <= 1 or loss_cap <= 0:
            raise ValueError
        for value in (count, consecutive, count_cap, consecutive_cap):
            if value < 0 or value != value.to_integral_value():
                raise ValueError
        checks = [
            (pnl <= -loss_cap, "daily_loss"),
            (rate >= rate_cap, "error_rate"),
            (count >= count_cap, "order_count"),
            (consecutive >= consecutive_cap, "consecutive_errors"),
        ]
        reasons = [reason for tripped, reason in checks if tripped]
        return {"trip": bool(reasons), "reasons": reasons}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return {"trip": True, "reasons": ["unparseable"]}
