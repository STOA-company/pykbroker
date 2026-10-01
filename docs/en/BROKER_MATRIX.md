---
description: "Offline contracts for KIS, DBFI and Toss, registered Kiwoom, locked LS and experimental NH/KB; KR/US markets and cancellation limits."
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
| Kiwoom | KR (KRX), limit orders | Supported (acknowledgement only) | Unsupported | Unsupported | Registered REST adapter; offline contracts |
| NH | KR skeleton | Unimplemented | Unimplemented | Unsupported | `NH_SPEC_VERIFIED = False` |
| KB | KR skeleton | Unimplemented | Unimplemented | Unsupported | `KB_SPEC_VERIFIED = False` |

KIS provides orders eligible for revision/cancellation; Toss provides open orders. An order disappearing from these lists is not evidence of a fill. DB queries validate and merge continuation pages returned by the SDK. DB remaining quantity of zero alone does not establish a final status.

KIS/DB US orderable cash varies by account type and requires an injected `cash_provider`. US buy/sell orders expose limit orders only. The library does not infer NXT, product, or market capabilities for an account.

TODO: LS US request mappings lacked verified enum values in the source; only domestic mappings were carried over. Kiwoom US is not implemented. Expansion requires official specifications and fixtures. NH/KB require endpoints and TR definitions and cannot run merely by changing the lock. LS query results remain unverified reference data.

## Kiwoom — BS-07 (2026-10-01)

`create_broker("kiwoom", ...)` uses the existing REST adapter with a stored token and app key. Construction never issues a token. Order/cancel transmission remains disabled by default (`OrderSafety` dry run); no new environment toggle enables it. `virtual=True` selects the mock host. Direct amendment is absent from the common SDK contract; amendment ledger fixtures leave an affected original order unknown unless fully executed.

Price evidence is offline: a `+` prefix keeps the magnitude; a `-` prefix raises `SpecUnverifiedError` for quote, book, band, position and open-order prices. Negative cash and quantities remain invalid. The official samples do not establish the meaning of `-`; this change does not infer it or take an absolute value.

The existing `examples/kiwoom_mock_roundtrip.py` is dry-run by default. With `--execute` and missing/blank mock keys it prints “키 없음”, returns `missing_keys` and exits 0 without constructing a client. That status means skipped, not successful. The mocked order/query/cancel round trip is tested; no broker API was called. Main must perform the actual mock round trip after H-KIWOOM issuance and independently review the PR.

See [BS-07 evidence pack](../status/2026-10-01-pack-kiwoom-bs07.md).

[한국어](../BROKER_MATRIX.md)

LS BS-17 (2026-10-01): `brokers/ls.py` and the factory are registered, with writes
locked by default. No transport, credentials, or TR budgets are supplied automatically.
`experimental.ls` aliases the same module and lock. Candidate `t0425.cheqty/cheprice`
fill quantity/average price semantics are **UNVERIFIED**. Locked responses retain
`unknown` status; missing execution price is `None`, never the order limit price.
Realtime orders, history, and virtual-account support remain unclaimed pending
specification comparison and H-LS. All LS fixtures are synthetic; see
`tests/fixtures/ls/README.md` for provenance and unverified fields.
