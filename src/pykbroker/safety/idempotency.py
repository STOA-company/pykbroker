"""Atomic client-key claims. Uncertain submissions retain their claim."""

import hashlib
import json
import threading
from decimal import Decimal
from typing import Protocol

from pykbroker.exceptions import ConfigurationError


class IdempotencyStore(Protocol):
    def claim(self, scope: str, key: str) -> bool:
        """Atomically persist first claim; duplicates return False. Never release on timeout."""
        ...


class MemoryIdempotencyStore:
    """Explicit single-process test/demo store; lost on restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()

    def claim(self, scope: str, key: str) -> bool:
        if not key.strip():
            raise ConfigurationError("A nonempty client key is required")
        with self._lock:
            entry = (scope, key)
            if entry in self._seen:
                return False
            self._seen.add(entry)
            return True


def order_key(
    *,
    account: str,
    broker: str,
    symbol: str,
    side: str,
    quantity: Decimal | int | str,
    cycle: str,
    price: Decimal | int | str | None = None,
    order_type: str = "limit",
) -> str:
    """Derive a key for one intent; cycle distinguishes repeated intentional orders."""

    def number(value: Decimal | int | str | None) -> str | None:
        if value is None:
            return None
        result = Decimal(str(value))
        if not result.is_finite():
            raise ConfigurationError("Idempotency numbers must be finite")
        return format(result.normalize(), "f")

    payload = [account, broker, symbol, side, number(quantity), cycle, number(price), order_type]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()[:36]


def scope_key(broker: str, account: str) -> str:
    return hashlib.sha256(json.dumps([broker, account]).encode()).hexdigest()
