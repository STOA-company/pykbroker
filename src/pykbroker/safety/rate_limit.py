"""Caller-supplied TR budgets, shared by credential digest within a process."""

import hashlib
import math
import threading
import time
from collections.abc import Callable, Mapping

from pykbroker.exceptions import ConfigurationError


class RateLimiter:
    def __init__(
        self,
        rates: Mapping[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock, self._sleep = clock, sleeper
        self._lock = threading.Lock()
        self._rates: dict[str, float] = {}
        self._next: dict[str, float] = {}
        self.register(rates or {})

    def register(self, rates: Mapping[str, float]) -> None:
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in rates.values()):
            raise ConfigurationError("TR rates must be positive and finite")
        with self._lock:
            for name, value in rates.items():
                self._rates[name] = min(value, self._rates.get(name, value))

    def wait(self, tr: str) -> None:
        with self._lock:
            if tr not in self._rates:
                raise ConfigurationError("TR rate has not been supplied")
            now = self._clock()
            deadline = self._next.get(tr, now)
            while now < deadline:
                self._sleep(deadline - now)
                now = self._clock()
            self._next[tr] = now + 1 / self._rates[tr]


_registry_lock = threading.Lock()
_registry: dict[tuple[str, str], RateLimiter] = {}


def credential_rate_limiter(
    broker: str,
    credential: str,
    rates: Mapping[str, float],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> RateLimiter:
    if not credential:
        raise ConfigurationError("A credential is required to scope rate limiting")
    key = (broker, hashlib.sha256(credential.encode()).hexdigest())
    with _registry_lock:
        if key not in _registry:
            _registry[key] = RateLimiter(rates, clock=clock, sleeper=sleeper)
        else:
            _registry[key].register(rates)
        return _registry[key]
