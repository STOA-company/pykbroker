---
description: "Default query tools in pykbroker-mcp, limits.toml order-tool registration, dry-run blocking and SQLite ledger behavior."
lang: en
---

# MCP server

`pykbroker-mcp` uses FastMCP's `@app.tool()`, JSON-string results, and stdio transport. Default tools are `get_quote`, `get_balance`, `get_positions`, `list_orders`, and `get_rules`. The package exposes `create_server()` and the module-level `mcp`. See the [Claude Desktop setup guide](claude-desktop.md) for local configuration.

## Registering order tools

At startup the server reads `~/.pykbroker/limits.toml`. All of the following are required to register `place_order` and `cancel_order`:

1. A readable, valid TOML file.
2. `allow_orders` is boolean `true`, not a string.
3. `per_order_krw_cap` and `daily_krw_cap` are finite positive amounts.
4. `dry_run` is boolean and defaults to `true` if omitted.

Empty files, incorrect types, missing limits, or invalid amounts hide order tools. A dry-run invocation raises `DryRunError` rather than returning a fabricated successful order. No usable cap file is supplied before users choose their own limits.

This is a **template requiring user input**, not directly executable TOML:

```text
allow_orders = true
per_order_krw_cap = <user-selected per-order KRW cap>
daily_krw_cap = <user-selected daily KRW cap>
dry_run = true
```

Deleting or editing the file after registration blocks orders and cancellations. Restart the server to update the tool list with reviewed settings. Changing settings does not retroactively cancel an in-flight vendor request.

## Order calls and storage

Sequence: input validation → dry-run → cap reservation → idempotency claim → settings recheck/kill switch → optional circuit breaker → vendor call.

- The default MCP command uses KR orders; it does not automatically configure US trading.
- `place_order(symbol, side, quantity, client_key, price, order_type="limit")`.
- `cancel_order(order_id, symbol, quantity, client_key)` targets a single order.
- Market orders require trusted upper-bound KRW valuation and session authorization. Default MCP does not infer these and blocks market orders.
- KIS single-order cancellation is explicitly unsupported. Toss cancels only the entire unfilled remainder of one order.

`orders.sqlite3` is created alongside the limits file on the first order invocation. Account identifiers and client_key values are hashed. Cap reservation and key claim are separate atomic transactions. Interruption between them may consume a cap without submitting an order. Duplicate requests, rejections, and timeouts do not refund reservations, so caps are consumed more conservatively than filled amounts.

The daily cap is the **sum of submitted value per account, broker, and KST date**. Buys and sells share that total. Cancellation checks the cap configuration with amount zero and uses a separate cancellation key namespace. KIS/DB do not claim native idempotency-key support. Local SQLite coordinates only processes on one host using the same file; multiple hosts require an injected shared store.

## Configure within an application

```python
from pykbroker import DBFI
from pykbroker.mcp.server import create_server

app = create_server(
    broker_factory=lambda safety: DBFI(
        account=caller_account, client=caller_client, safety=safety
    )
)
app.run(transport="stdio")
```

The injected factory must return a `BaseBroker` bound to the exact supplied `OrderSafety`; factories replacing it are rejected. The constructed adapter is closed at shutdown. Logs contain broker, masked account, and action only, without raw requests, responses, or keys.

Toss SDK dry-run and live flags also apply independently. The default factory does not disable those additional guards. Supply Toss live settings only through an explicitly configured caller-owned `broker_factory`.

[한국어](../mcp.md)
