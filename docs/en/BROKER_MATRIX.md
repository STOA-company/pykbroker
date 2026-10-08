---
description: "Offline contracts for KIS, DBFI and Toss; registered Kiwoom/LS/NH, LS writes limited to verified mock instances, NH live orders SDK-locked by default, KB outside the factory; KR/US markets and cancellation limits."
lang: en
---

# Broker support matrix

This describes the extracted implementation as of 2026-10-01, with Kiwoom changes as of 2026-10-08. All validation uses mocks and synthetic fixtures for offline contract checks. **Real APIs, paper accounts, and live trading have not been validated.**

| Adapter | Query/order markets | Single-order cancellation | Market orders | NXT | Status |
| --- | --- | --- | --- | --- | --- |
| KIS | KR / US | Unsupported | KR, separate session and valuation authorization required | Unsupported | Conversion implementation and offline tests |
| DBFI | KR / US | Supported | KR, separate session and valuation authorization required | KR | Conversion implementation and offline tests |
| Toss | KR | Entire unfilled remainder | KR, separate session and valuation authorization required | Unsupported | SDK bridge checked with mocks |
| LS | KR | Mapped, locked | Mapped, locked | Unsupported | Formal module/factory, `LS_SPEC_VERIFIED = False` |
| Kiwoom | KR (KRX) / US, limit orders | Supported (KR confirmed quantities reconciled; US full remainder) | Unsupported | Unsupported | Registered REST adapter; offline contracts |
| NH | KR (KRX/NXT), cash limit orders | Supported (acknowledgement only) | Unsupported | KRX/NXT | Registered REST adapter; offline contracts; live orders locked by default |
| KB | KR skeleton | Unimplemented | Unimplemented | Unsupported | `KB_SPEC_VERIFIED = False` |

KIS provides orders eligible for revision/cancellation; Toss provides open orders. An order disappearing from these lists is not evidence of a fill. DB queries validate and merge continuation pages returned by the SDK. DB remaining quantity of zero alone does not establish a final status.

KIS/DB US orderable cash varies by account type and requires an injected `cash_provider`. US buy/sell orders expose limit orders only. The library does not infer NXT, product, or market capabilities for an account.

TODO: LS US request mappings lacked verified enum values in the source; only domestic mappings were carried over. Kiwoom US already exists in the starting HEAD; this KR change does not add it. NH US queries/orders remain unimplemented; actual API response checks and order-to-ledger reconciliation remain unverified. KB requires endpoints and TR definitions. LS query results remain unverified reference data.

## Kiwoom (2026-10-08)

`create_broker("kiwoom", ...)` uses the existing REST adapter with a stored token and app key. Construction never issues a token. Order/cancel transmission remains disabled by default (`OrderSafety` dry run); no new environment toggle enables it. `virtual=True` selects the mock host. Direct amendment is absent from the common SDK contract; amendment ledger fixtures leave an affected original order unknown unless fully executed.

Official ka10095 lower limit `-85200` and ka10001 yearly low `-91200` establish direction-signed price strings: `+` and `-` preserve magnitude. Band consistency checks remain; quantity parsing receives no price-sign conversion.

The same magnitude conversion applies to US quotes, bid/ask prices, holding cost/current prices, order prices and execution unit prices. Pinned official US examples include usa20100's yearly low `-164.2700`, usa20101, ust21070 and ust21050; negative-price tests derive down-day cases by changing price signs only. US down-day prices previously rejected are now read as absolute values, opening the order path for those instruments. US rate budgets, cash handling, halt checks and order safety gates remain unchanged.

KR cash is the minimum of kt00001 `ord_alow_amt`, `entr`, `100stk_ord_alow_amt`, and `d2_entra`, with deficits clamped to zero (official example: KRW 12,550). KR `orderable_quantity` reads kt00011 `min_ord_alowq`. Open-order average execution price uses positive ka10075 `cntr_tot_amt / cntr_qty`. Confirmed kt00007 cancel rows must link the original number, symbol and KRX, and conserve ordered = filled + confirmed cancelled + remaining quantities. Zero remainder with a confirmed cancellation is `cancelled`.

A positive original quantity with filled = ordered and zero remainder is `filled`, regardless of later amendment/cancel labels. Partial fills retain the cancel-quantity conservation rules above; unrecognized amendment/cancel labels remain `unknown`.

Legacy `experimental.kiwoom.get_balance` is blocked before transport because it returned estimated asset value as cash. Use the registered adapter. The official 1700–1702 errors are flow limits; this HEAD has no retryable-rejection classification, so existing `BrokerRejectedError` remains without automatic retries.

The existing `examples/kiwoom_mock_roundtrip.py` is dry-run by default. With `--execute` and missing/blank mock keys it prints “키 없음”, returns `missing_keys` and exits 0 without constructing a client. That status means skipped, not successful. The mocked order/query/cancel round trip is tested; no broker API was called. The actual mock round trip and independent review remain pending mock-key issuance.

See [KR order change pack](../status/2026-10-08-pack-kiwoom-kr-live.md) and [official provenance](../../tests/fixtures/kiwoom/official/SOURCE.md).

## NH (2026-10-01)

`create_broker("nh", ...)` accepts a stored token or app key/secret. Declare `acct_type`: `03` for mock, `01`/`02` for live accounts. The adapter checks injected client hosts immediately after construction. Mock order/cancel and account data use moapi; token issuance, account verification, quotes and rules use api. The official specification marks quotes as production-only; this remains **UNVERIFIED** until actual API responses are checked.

Live orders/cancellations are locked by default (`allow_live_orders=False`). Enabling `OrderSafety` or MCP limits alone cannot unlock NH. The explicit constructor override is reserved for use after order-to-ledger reconciliation is verified and separately authorized, and existing safety gates still apply. Mandatory account ownership verification (`verify_account`) will be addressed with order-to-ledger reconciliation.

Account mode `virtual` is read-only; the verified order host determines the live lock and order mode. Reads require a known success code and the relevant data block; failed-completion wording is rejected. A blockless `13578` response means empty positions/order history or zero sellable quantity, while cash reads still require a summary. Unknown codes are rejected even with completion wording. Corrupt shared expiry is ignored and replaced by a fresh token; newer shared tokens are preserved and reused. HTTP redirects are refused. Placement `mkt:` IDs remain unlinked to ledger `itg:` IDs, so post-placement status is unknown.

See the [NH evidence pack](../status/2026-10-01-pack-nh.md) and [official quote field excerpt](../contracts/nh-current-price-fields.json). No broker API was called.

[한국어](../BROKER_MATRIX.md)

## LS (2026-10-01)

`brokers/ls.py` and `create_broker("ls", ...)` are registered; `experimental.ls` aliases the same module. Construction requires an injected client with an explicit `base_url`. No default client, credentials, TR budgets, or enum values are provided.

**Orders and cancellations on live bindings remain locked.** `virtual` accepts only booleans; the host is checked immediately after construction and before requests. `LS_SPEC_VERIFIED=False` is a global veto. Changing it never unlocks a live binding. The distinct mock address contract is **UNVERIFIED** and unset, so `virtual=True` construction is also rejected. The adapter does not infer a mock address from the live address.

Offline tests supply a fictitious mock host and explicit instance `spec_verified=True` to exercise the existing safety gate. The default is `False` and verification never spreads to other instances. Returned orders use the account mode checked at construction.

`t0425.qty/cheqty/cheprice` semantics are **UNVERIFIED**. Locked reads keep `quantity`, `filled`, and `fill_price` as `None` and `status` as `unknown`. Even mock verification leaves `fill_price` unmapped until `cheprice` is established as the average execution price. Realtime orders, history, and virtual-account support remain unclaimed. All LS fixtures are synthetic.
