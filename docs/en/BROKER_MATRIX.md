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

## Kiwoom (2026-10-01)

`create_broker("kiwoom", ...)` uses the existing REST adapter with a stored token and app key. Construction never issues a token. Order/cancel transmission remains disabled by default (`OrderSafety` dry run); no new environment toggle enables it. `virtual=True` selects the mock host. Direct amendment is absent from the common SDK contract; amendment ledger fixtures leave an affected original order unknown unless fully executed.

Price evidence is offline: a `+` prefix keeps the magnitude; a `-` prefix raises `SpecUnverifiedError` for quote, book, band, position and open-order prices. Negative cash and quantities remain invalid. The official samples do not establish the meaning of `-`; this change does not infer it or take an absolute value.

The existing `examples/kiwoom_mock_roundtrip.py` is dry-run by default. With `--execute` and missing/blank mock keys it prints “키 없음”, returns `missing_keys` and exits 0 without constructing a client. That status means skipped, not successful. The mocked order/query/cancel round trip is tested; no broker API was called. The actual mock round trip and independent review remain pending mock-key issuance.

See [Kiwoom evidence pack](../status/2026-10-01-pack-kiwoom-bs07.md).

[한국어](../BROKER_MATRIX.md)

## LS (2026-10-01)

`brokers/ls.py` and `create_broker("ls", ...)` are registered; `experimental.ls` aliases the same module. Construction requires an injected client with an explicit `base_url`. No default client, credentials, TR budgets, or enum values are provided.

**Orders and cancellations on live bindings remain locked.** `virtual` accepts only booleans; the host is checked immediately after construction and before requests. `LS_SPEC_VERIFIED=False` is a global veto. Changing it never unlocks a live binding. The distinct mock address contract is **UNVERIFIED** and unset, so `virtual=True` construction is also rejected. The adapter does not infer a mock address from the live address.

Offline tests supply a fictitious mock host and explicit instance `spec_verified=True` to exercise the existing safety gate. The default is `False` and verification never spreads to other instances. Returned orders use the account mode checked at construction.

`t0425.qty/cheqty/cheprice` semantics are **UNVERIFIED**. Locked reads keep `quantity`, `filled`, and `fill_price` as `None` and `status` as `unknown`. Even mock verification leaves `fill_price` unmapped until `cheprice` is established as the average execution price. Realtime orders, history, and virtual-account support remain unclaimed. All LS fixtures are synthetic.
