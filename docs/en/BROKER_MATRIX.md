---
description: "Offline verification scope for KIS, DBFI and Toss, registered Kiwoom/NH and experimental LS/KB status, KR/US markets and cancellation limits."
lang: en
---

# Broker support matrix

This describes the extracted implementation as of 2026-10-01. All validation uses mocks and synthetic fixtures for offline contract checks. **Real APIs, paper accounts, and live trading have not been validated.**

| Adapter | Query/order markets | Single-order cancellation | Market orders | NXT | Status |
| --- | --- | --- | --- | --- | --- |
| KIS | KR / US | Unsupported | KR, separate session and valuation authorization required | Unsupported | Conversion implementation and offline tests |
| DBFI | KR / US | Supported | KR, separate session and valuation authorization required | KR | Conversion implementation and offline tests |
| Toss | KR | Entire unfilled remainder | KR, separate session and valuation authorization required | Unsupported | SDK bridge checked with mocks |
| LS | KR | Locked | Unverified | Unsupported | `LS_SPEC_VERIFIED = False` |
| Kiwoom | KR (KRX), limit orders | Supported (acknowledgement only) | Unsupported | Unsupported | Registered REST adapter; offline contracts |
| NH | KR (KRX/NXT), cash limit orders | Supported (acknowledgement only) | Unsupported | KRX/NXT | Registered REST adapter; offline contracts; live orders locked by default |
| KB | KR skeleton | Unimplemented | Unimplemented | Unsupported | `KB_SPEC_VERIFIED = False` |

KIS provides orders eligible for revision/cancellation; Toss provides open orders. An order disappearing from these lists is not evidence of a fill. DB queries validate and merge continuation pages returned by the SDK. DB remaining quantity of zero alone does not establish a final status.

KIS/DB US orderable cash varies by account type and requires an injected `cash_provider`. US buy/sell orders expose limit orders only. The library does not infer NXT, product, or market capabilities for an account.

TODO: LS US request mappings lacked verified enum values in the source; only domestic mappings were carried over. Kiwoom US is not implemented. Expansion requires official specifications and fixtures. NH US queries/orders remain unimplemented; actual API response checks and order-to-ledger reconciliation remain unverified. KB requires endpoints and TR definitions. LS query results remain unverified reference data.

## Kiwoom — BS-07 (2026-10-01)

`create_broker("kiwoom", ...)` uses the existing REST adapter with a stored token and app key. Construction never issues a token. Order/cancel transmission remains disabled by default (`OrderSafety` dry run); no new environment toggle enables it. `virtual=True` selects the mock host. Direct amendment is absent from the common SDK contract; amendment ledger fixtures leave an affected original order unknown unless fully executed.

Price evidence is offline: a `+` prefix keeps the magnitude; a `-` prefix raises `SpecUnverifiedError` for quote, book, band, position and open-order prices. Negative cash and quantities remain invalid. The official samples do not establish the meaning of `-`; this change does not infer it or take an absolute value.

The existing `examples/kiwoom_mock_roundtrip.py` is dry-run by default. With `--execute` and missing/blank mock keys it prints “키 없음”, returns `missing_keys` and exits 0 without constructing a client. That status means skipped, not successful. The mocked order/query/cancel round trip is tested; no broker API was called. Main must perform the actual mock round trip after H-KIWOOM issuance and independently review the PR.

See [BS-07 evidence pack](../status/2026-10-01-pack-kiwoom-bs07.md).

## NH (2026-10-01)

`create_broker("nh", ...)` accepts a stored token or app key/secret. Declare `acct_type`: `03` for mock, `01`/`02` for live accounts. The adapter checks injected client hosts immediately after construction. Mock order/cancel and account data use moapi; token issuance, account verification, quotes and rules use api. The official specification marks quotes as production-only; this remains **UNVERIFIED** until actual API responses are checked.

Live orders/cancellations are locked by default (`allow_live_orders=False`). Enabling `OrderSafety` or MCP limits alone cannot unlock NH. The explicit constructor override is reserved for use after order-to-ledger reconciliation is verified and separately authorized, and existing safety gates still apply. Mandatory account ownership verification (`verify_account`) will be addressed with order-to-ledger reconciliation.

Account mode `virtual` is read-only; the verified order host determines the live lock and order mode. Reads require a known success code and the relevant data block; failed-completion wording is rejected. A blockless `13578` response means empty positions/order history or zero sellable quantity, while cash reads still require a summary. Unknown codes are rejected even with completion wording. Corrupt shared expiry is ignored and replaced by a fresh token; newer shared tokens are preserved and reused. HTTP redirects are refused. Placement `mkt:` IDs remain unlinked to ledger `itg:` IDs, so post-placement status is unknown.

See the [NH evidence pack](../status/2026-10-01-pack-nh.md) and [official quote field excerpt](../contracts/nh-current-price-fields.json). No broker API was called.

[한국어](../BROKER_MATRIX.md)
