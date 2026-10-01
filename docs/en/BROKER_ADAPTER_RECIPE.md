---
description: "Add a Korean broker adapter with documented response evidence, client injection, order safety gates and offline tests."
lang: en
---

# Adding a broker adapter

## 1. Contracts and evidence

Read the contracts in `base.py` and `models/` first. Confirm official responses and invocation methods for queries, orders, and cancellations; record documentation versions and fixture provenance. Clearly distinguish mock responses from recorded vendor responses. Do not substitute success values or zero for unknown fields or features.

## 2. Injection boundary

Subclass `BaseBroker` and implement vendor calls and model conversion only. Use only the account passed to the constructor. Use standard logging without request bodies, credentials, or actual account values. Accept clients or callbacks for authentication, token refresh, or database access. Defer SDK imports until a connection is needed.

## 3. Safety gate

Route public `place_order` and `cancel_order` through `OrderSafety` before calling `_place_order` and `_cancel_order`. Do not add order retries or quantity-reduction loops. Do not widen cancellation to every order for a symbol. Leave cancellation unsupported if its native scope is unverified.

Keep unverified adapters in `experimental/` with their module's `*_SPEC_VERIFIED = False`. Public order methods proceed only when that value is exactly `True`. Reject unknown enum values until the caller supplies verified `codes`. Changing a lock alone must never send an unverified request.

## 4. Lifetimes and responses

Synchronous adapters close owned clients on shutdown; callers manage injected clients. Async SDK construction, invocation, and shutdown belong to one event loop, with overlapping use prevented. Do not create a new loop per request.

Keep balances separate by currency. Absence from an open-order list is not fill evidence. Use `InvalidResponseError` or an `unknown` status for ambiguous order responses. If a page fails, do not return a partial balance as a complete balance.

## 5. Validation

Use the JSON fixture loader and network-denial fixture in `tests/conftest.py`. Minimum checks cover quotes, empty books, missing cash, failed pages, exact buy/sell arguments, cancellation scope, dry-run blocking, duplicate submissions, cap boundaries, import without vendor SDKs, and shutdown.

Confirm that mutations such as removing locks, swapping buy/sell codes, or bypassing a limiter fail tests. Update the support matrix and capabilities together after verification. Keep live trading validation outside this offline suite.

## 6. Before publication

Check source, documentation, and examples for secrets, company-specific addresses, and account storage schemas. Review the license and NOTICE, record only checks actually executed, and leave unfinished work in TODOs and the support matrix.

[한국어](../BROKER_ADAPTER_RECIPE.md)

LS task instructions (2026-10-01): promote LS to `brokers/ls.py` and the
factory using legacy mappings with explicit `UNVERIFIED` annotations even though
detailed guides were inaccessible. Keep `LS_SPEC_VERIFIED=False`, disabled order
capabilities, and no default enum values. LS additionally requires explicit instance
verification for mock writes; live-binding writes stay locked.
