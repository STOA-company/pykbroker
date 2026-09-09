"""Caller-owned halt state. No policy table or stored on/off values."""

from typing import Protocol

from pykbroker.exceptions import KillSwitchError


class HaltProvider(Protocol):
    def __call__(self, broker: str, account: str) -> bool: ...


class KillSwitch:
    def __init__(self, provider: HaltProvider | None = None) -> None:
        self._provider = provider

    def require_open(self, broker: str, account: str) -> None:
        try:
            halted = self._provider(broker, account) if self._provider is not None else None
        except Exception:
            raise KillSwitchError("kill_switch: status unavailable") from None
        if halted is not False:
            raise KillSwitchError("kill_switch: halted or unconfigured")
