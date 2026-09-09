"""Explicit opt-in only. No environment or application policy is assumed."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pykbroker.exceptions import DryRunError


@dataclass(frozen=True)
class DryRun:
    enabled: bool = True

    def require_live(self) -> None:
        if self.enabled is not False:
            raise DryRunError("dry_run: live operations are disabled")


def should_execute_live(config: Any = None) -> bool:
    return isinstance(config, Mapping) and config.get("dry_run") is False


def mask_account(value: object) -> str:
    text = str(value)
    return "****" + (text[-4:] if len(text) > 4 else "")


def redact_order_for_log(value: Any) -> Any:
    """Only structured, explicitly allowed scalar fields may reach audit logs."""
    allowed = {"broker", "action", "status", "reason"}
    if isinstance(value, Mapping):
        return {
            str(k): (
                mask_account(v) if k == "account" else v if k in allowed and isinstance(v, (str, bool, int)) else "****"
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_order_for_log(item) for item in value]
    return "****"
