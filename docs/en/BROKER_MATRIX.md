---
description: "Offline verification scope for KIS, DBFI and Toss, experimental LS/Kiwoom/NH/KB status, KR/US markets and cancellation limits."
lang: en
---

# Broker support matrix

This describes the extracted implementation as of 2026-09-09. All validation uses mocks and synthetic fixtures for offline contract checks. **Real APIs, paper accounts, and live trading have not been validated.**

| Adapter | Query/order markets | Single-order cancellation | Market orders | NXT | Status |
| --- | --- | --- | --- | --- | --- |
| KIS | KR / US | Unsupported | KR, separate session and valuation authorization required | Unsupported | Conversion implementation and offline tests |
| DBFI | KR / US | Supported | KR, separate session and valuation authorization required | KR | Conversion implementation and offline tests |
| Toss | KR / US | Entire unfilled remainder | KR, separate session and valuation authorization required | Unsupported | SDK bridge checked with mocks |
| LS | KR | Locked | Unverified | Unsupported | `LS_SPEC_VERIFIED = False` |
| Kiwoom | KR | Unimplemented | Unverified | Unsupported | `KIWOOM_SPEC_VERIFIED = False` |
| NH | KR skeleton | Unimplemented | Unimplemented | Unsupported | `NH_SPEC_VERIFIED = False` |
| KB | KR skeleton | Unimplemented | Unimplemented | Unsupported | `KB_SPEC_VERIFIED = False` |

KIS provides orders eligible for revision/cancellation; Toss provides open orders. An order disappearing from these lists is not evidence of a fill. DB queries validate and merge continuation pages returned by the SDK. DB remaining quantity of zero alone does not establish a final status.

KIS/DB US orderable cash varies by account type and requires an injected `cash_provider`. US buy/sell orders expose limit orders only. The library does not infer NXT, product, or market capabilities for an account.

TODO: LS/Kiwoom US request mappings lacked verified enum values in the source; only domestic mappings were carried over. Expansion requires official specifications and fixtures. NH/KB require endpoints and TR definitions and cannot run merely by changing the lock. LS/Kiwoom query results are also unverified reference data and are excluded from the registered implementation count.

[한국어](../BROKER_MATRIX.md)
