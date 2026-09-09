---
description: "Immutable Pydantic models for balances, quotes, orders, cancellations and trading rules, including Decimal and unknown-value semantics."
lang: en
---

# Models

Models are immutable Pydantic v2 objects that reject unknown fields. Amounts remain `Decimal` and serialize to JSON strings. Integer quantities reject booleans, strings, and fractional values.

| Model | Key fields |
| --- | --- |
| `Balance` | currency, cash_available, position_value; total property |
| `Position` | symbol, market, quantity, sellable, average_price, last, market_value |
| `Quote` | symbol, market, last, bid, ask, prev_close, timestamp |
| `OrderRequest` | symbol, side, quantity, order_type, price, market, venue, client_key |
| `CancelRequest` | order_id, symbol, quantity, market, venue, client_key |
| `Order` | id, symbol, status, quantity, price, filled, remaining |
| `Rules` | symbol, market, currency, tick, lot, min_notional, regular_open, regular_close, timezone |
| `Capabilities` | Markets, single-order cancellation, market orders, NXT, and order-history support |

`None` means unverified or unknown. `accepted` means request acceptance, not a fill or final cancellation. Models do not store raw responses, accounts, or keys. `client_key` is hidden from repr but is included if a caller serializes the entire model; do not log that serialization.

[한국어](../../api/models.md)
