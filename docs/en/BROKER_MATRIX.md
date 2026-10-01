---
description: "Offline verification scope for KIS, DBFI and Toss, locked LS and experimental Kiwoom/NH/KB status, KR/US markets and cancellation limits."
lang: en
---

# Broker support matrix

This describes the extracted implementation as of 2026-09-09. All validation uses mocks and synthetic fixtures for offline contract checks. **Real APIs, paper accounts, and live trading have not been validated.**

| Adapter | Query/order markets | Single-order cancellation | Market orders | NXT | Status |
| --- | --- | --- | --- | --- | --- |
| KIS | KR / US | Unsupported | KR, separate session and valuation authorization required | Unsupported | Conversion implementation and offline tests |
| DBFI | KR / US | Supported | KR, separate session and valuation authorization required | KR | Conversion implementation and offline tests |
| Toss | KR | Entire unfilled remainder | KR, separate session and valuation authorization required | Unsupported | SDK bridge checked with mocks |
| LS | KR | Mapped, locked | Mapped, locked | Unsupported | Formal module/factory, `LS_SPEC_VERIFIED = False` |
| Kiwoom | KR | Unimplemented | Unverified | Unsupported | `KIWOOM_SPEC_VERIFIED = False` |
| NH | KR skeleton | Unimplemented | Unimplemented | Unsupported | `NH_SPEC_VERIFIED = False` |
| KB | KR skeleton | Unimplemented | Unimplemented | Unsupported | `KB_SPEC_VERIFIED = False` |

KIS provides orders eligible for revision/cancellation; Toss provides open orders. An order disappearing from these lists is not evidence of a fill. DB queries validate and merge continuation pages returned by the SDK. DB remaining quantity of zero alone does not establish a final status.

KIS/DB US orderable cash varies by account type and requires an injected `cash_provider`. US buy/sell orders expose limit orders only. The library does not infer NXT, product, or market capabilities for an account.

TODO: LS/Kiwoom US request mappings lacked verified enum values in the source; only domestic mappings were carried over. Expansion requires official specifications and fixtures. NH/KB require endpoints and TR definitions and cannot run merely by changing the lock. LS/Kiwoom query results are also unverified reference data and are excluded from the registered implementation count.

[한국어](../BROKER_MATRIX.md)

LS BS-17 (2026-10-01): `brokers/ls.py` and the factory are registered, with writes
locked by default. No transport, credentials, or TR budgets are supplied automatically.
`experimental.ls` aliases the same module and lock. Candidate `t0425.cheqty/cheprice`
fill quantity/average price semantics are **UNVERIFIED**. Locked responses retain
`unknown` status; missing execution price is `None`, never the order limit price.
Realtime orders, history, and virtual-account support remain unclaimed pending
specification comparison and H-LS. All LS fixtures are synthetic; see
`tests/fixtures/ls/README.md` for provenance and unverified fields.
