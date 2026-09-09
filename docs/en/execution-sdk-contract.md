---
description: "Fixed-context, quantity, quote, order, safety and HTTP contracts for engine implementers. pykbroker supplies no execution engine."
lang: en
---

# Execution SDK contract

This document and `pykbroker.contracts.execution` define **contracts** for an execution engine. This package contains no engine, code execution, intent dispatch, restart recovery, or scheduler. None of these contract functions sends executable code to the trader.

## 1. Execution boundary and fixed context

Each execution is bound to **one account, one symbol, one confirmed quantity, and one session**. After user confirmation, account, symbol, side, target quantity, and selected session cannot change; changes require confirmation of a new execution. The account is a read-only snapshot refreshed once at execution start, enforced through a deep copy or immutable storage.

| Property | Contract type | Meaning |
| --- | --- | --- |
| `ctx.account` | `Account` | `account_id`, `broker`, `cash_available`, `holdings` |
| `ctx.symbol` | `Symbol` | `code`, `name`, `market: KR/US/COIN`, `venue`, `tick`, `lot`, `min_notional` |
| `ctx.side` | `buy / sell` | One direction |
| `ctx.target_qty()` | `int` | Confirmed total shares |
| `ctx.remaining()` | `int` | Target minus fills belonging to this execution |
| `ctx.filled_qty()` | `int` | Cumulative fills for this execution |
| `ctx.filled_avg()` | `float` | Weighted average fill price for this execution |

Do not infer state from account-wide fills or changes in holdings. Compute cumulative and remaining quantities from the fill ledger associated with the execution ID. The engine must consistently define and document average-price representation before any fills.

The table's `ctx` quantities are integer shares. Intent transport types permit float COIN quantities, but this does not extend the package's stock brokers or `ctx` calculations to fractional coin quantities.

## 2. Quantity calculation

Pure calculations do not create order intents. This boundary prevents calculations from silently changing the order the user confirmed.

| Function | Return | Engine rule |
| --- | --- | --- |
| `qty_for_krw(krw: int, price: float)` | `int` shares | `floor(krw / price)`; zero raises `Clarify("Amount is below the price of one share")` |
| `split(total: int, n: int, i: int)` | `int` shares | Equal splits, remainder in the final round; `1 ≤ n ≤ 40`; this contract uses `0 ≤ i < n` |
| `round_price(price: float)` | `int` KRW | Round buys down and sells up to the symbol's tick |
| `held()` | `int` shares | Holdings used as the sell-quantity ceiling |

Nonpositive prices, invalid split counts, and missing symbol rules must produce `Clarify` during confirmation. Tick and quantity calculations are implemented by the engine. `rules.kr` is a separate market-rule helper, not justification for adding an engine here.

## 3. Reading market data

Query through the trader quote proxy with **at most one second of staleness and two queries per second per execution**. Do not replace stale or missing prices with zero for quantity calculations.

| Function | Return |
| --- | --- |
| `last()` | Current price `float` |
| `best_bid()` / `best_ask()` | Best bid/ask `float` |
| `prev_close()` | Previous close `float`, the price-band reference |
| `vwap_today()` | Cumulative intraday VWAP from one-minute bars, `float` |
| `bars(n: int)` | Most recent n one-minute bars, `list[Bar]`, `n ≤ 390` |

`Bar` contains `t, o, h, l, c, v`. Times and prices must match the symbol's trading date and currency. The engine owns query limits, freshness validation, and calculations. Do not assume the package's `Quote` provides all proxy metrics.

## 4. Time and waiting

| Function | Return or rule |
| --- | --- |
| `now()` | Timezone-aware KST `datetime` |
| `session()` | `Session{open, close, is_open}`; the next trading-day session selected at confirmation stays fixed |
| `wait(duration: str)` | `"30s"` to `"120m"`, minimum 30 seconds; reaching close raises `ExecutionEnded` |
| `wait_until(hhmm: str)` | Only a time within the selected session; otherwise build-time `Clarify` |
| `wait_for(pred, timeout: str)` | Evaluate `pred()` every 30 seconds |

The engine supplies a calendar including holidays and special opening days. Compare against the selected session; do not infer trading dates from server UTC dates or weekdays. Waiting, polling, and resumption belong to the engine; this package supplies signatures only.

## 5. Orders and state

**One order function corresponds to one trader intent.** Track partial fills and cancellations by intent and execution ID; do not automatically turn timeouts into new orders.

| Function | Return | Trader intent / rule |
| --- | --- | --- |
| `place_limit(qty, price, ttl="5m")` | `Order` | `kind="limit"`, price, ttl_sec; TTL is 1–60 minutes or `until_close` |
| `place_market(qty)` | `Order` | `kind="market"`; regular session only |
| `cancel(order)` | None | Cancel only that intent's unfilled remainder |
| `cancel_open()` | None | Cancel all open intents in this execution; automatically invoked at execution end |
| `order.status()` | `Status` | Identify this intent in the execution query result |
| `order.wait(timeout)` | `Status` | Poll until filled, cancelled, or timed out |

`Status` contains `state`, `filled_qty`, and `avg_price`; state is `open`, `partial`, `filled`, `cancelled`, or `rejected`. This contract's `Order` is a behavioral interface separate from the SDK's Pydantic `models.Order`. The engine implements status and waiting.

Convert `until_close` into the remaining time until the fixed session closes. Do not add a limit price to a market intent or widen single-order cancellation to an account or symbol. A cancellation response does not undo fills.

## 6. Recording and shutdown

| Function | Meaning |
| --- | --- |
| `note(text)` | One user-visible execution log line |
| `done(reason)` | End early; automatically cancel open intents |

The engine must remove account/credential values from records and handle duplicate shutdown safely. It also owns reconciliation of cancellation results against the ledger after shutdown requests. The contract package does not execute logging storage or cancellation loops.

## 7. Exceptions and safety gates

| Exception | Trigger | Engine handling |
| --- | --- | --- |
| `Clarify(question)` | Missing values or out-of-session settings at build/dry-run time | Ask the user for clarification |
| `Rejected(reason)` | Intent rejected by caps, price bands, kill switch, session checks, etc. | User code may catch it; otherwise execution ends |
| `ExecutionEnded` | Market close, runtime limit, or user cancellation | Cancel open intents, then stop |

The trader must validate quantity, ownership, session, price bands, dry-run, caps, idempotency, and rate limits. Context or SDK prevalidation does not replace server-side authorization and gates. The caller supplies actual limits and approval policy.

## 8. Trader HTTP contract

The following paths are **implementation requirements**, not HTTP endpoints registered by pykbroker.

```text
POST /orders/intents
{
  execution_id, seq, account_id, ticker,
  market: "KR" | "US" | "COIN", venue?, side: "buy" | "sell",
  qty, kind: "limit" | "market", price, ttl_sec, client_key
}
```

- KR/US `qty` is integer; COIN uses real numbers. The server validates positive, finite prices and quantities.
- `execution_id` scopes the execution, `seq` is its internal sequence, and `client_key` identifies retransmission of the same intent.
- Account ID is a caller-supplied opaque contract identifier without assumptions about a repository's column schema.
- Limit intents require price; market intents do not imply a limit-price constraint.

```text
200 { intent_id, status: "accepted", broker_order_id }
409 { status: "rejected", reason:
      kill_switch | dry_run | not_owner | cap_exceeded |
      price_band | session_closed | rate_limit | duplicate }

POST /orders/intents/{intent_id}/cancel
GET  /orders/executions/{execution_id}
     → { intents[], filled_qty, avg_price, remaining }
GET  /quotes/{ticker}
     → { last, bid1, ask1, prev_close, vwap }
```

The `Trader` Protocol represents these four operations. Intent data carries numbers, strings, and state only, never user code, functions, or serialized executable code. Authentication, ownership checks, dispatch, fill synchronization, resumption, recovery, and gate policy implementation remain outside this package.

## 9. Validation criteria for engine implementers

Verify rejection of fixed-context edits, split totals and final remainders, tick-rounding direction, trading-day boundaries, stale quotes, query limits, out-of-session market orders, duplicate intents, partial-fill cancellation, expiry, shutdown cleanup, and authorization denial in the engine's own repository. This repository uses AST checks to ensure that all Protocol method bodies contain only `raise NotImplementedError`.

Documented function rules do not mean an execution engine exists. The Korean Markdown source and its existing HTML review copy describe the same contract; Markdown is authoritative. This English page translates that source.

[한국어](../execution-sdk-contract.md)
