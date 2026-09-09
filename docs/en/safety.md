---
description: "Default order/cancellation blocking and injection contracts for caps, idempotency, kill switches and optional circuit breakers."
lang: en
---

# Safety primitives

The package contains no company-specific policy numbers or account repository. Default inputs block operations.

| Primitive | Injection contract | Invalid input |
| --- | --- | --- |
| `DryRun` | Only exact boolean `False` permits live operations | Block |
| `KillSwitch` | `HaltProvider(broker, account) -> bool`; only `False` permits continuation | Block |
| `RiskLimits` / `RiskLedger` | Per-order/daily KRW caps and atomic reservation storage | Block |
| `IdempotencyStore` | `claim(scope, key) -> bool`; only the first claim returns True | Block |
| `evaluate_breaker` | Loss/error rates, observation counts, and thresholds | Trip |
| `RateLimiter` | Per-TR requests per second, clock, and sleep function | Block unregistered TR |

`OrderSafety` is the shared SDK/MCP submission boundary and defaults to dry-run. Live configuration requires limits, a usage ledger, an idempotency store, and a kill switch. Circuit breakers and rate limiters are separately configurable primitives. Experimental REST adapters refuse calls without a limiter. Existing KIS/DB/Toss rate limiting is the responsibility of the vendor SDK or injected client.

Limits use `Decimal`; KRW limit orders use `quantity × price`. If a separate valuation is injected, the larger amount applies. US, market, and after-hours orders require caller-supplied conservative KRW valuation. Market orders also require a regular-session authorization callback. A single current quote cannot guarantee a ceiling on market-order execution value.

`MemoryRiskLedger` and `MemoryIdempotencyStore` are explicitly selected, single-process test/demo stores whose records vanish on restart. MCP's default `SQLiteLedger` serializes reservations across processes using the same file. Neither approach reverses orders already received by a broker.

There are no automatic retries after errors. Reservations and claims persist on both success and failure. Reconcile ambiguous outcomes against the vendor order ledger before deciding whether there is a new intent. Do not delete the ledger to retry.

`order_key()` incorporates account, broker, symbol, side, quantity, price, order type, and round. Equivalent numeric representations yield the same key. Use a new round or client_key for a new intent, and preserve the existing key when invoking the same intent again.

[한국어](../safety.md)
