---
description: "Query and order contracts for KIS, DBFI, Toss and create_broker, instance lifetimes and experimental adapter restrictions."
lang: en
---

# Brokers

Import `KIS`, `DBFI`, `Toss`, and `create_broker()` from the top level. Public queries are `get_quote`, `get_balance`, `get_positions`, `list_orders`, and `get_rules`; mutations are `place_order` and `cancel_order`. The `BrokerAdapter` vocabulary also exposes `quote`, `balance`, `positions`, `rules`, `place`, `cancel`, `status`, and `close`.

Clients, accounts, and safety are explicitly bound per instance. Callers coordinate concurrent access per account; MCP serializes adapter access. See the [support matrix](../BROKER_MATRIX.md) and [installation guide](../getting-started.md).

`experimental` adapters are not registered in the top-level `BROKERS` factory. Import their modules directly; order locks are closed by default.

[한국어](../../api/brokers.md)
