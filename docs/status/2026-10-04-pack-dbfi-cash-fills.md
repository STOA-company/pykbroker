# DBFI buying power and execution prices (2026-10-04)

Status: source contract verified offline; no account, broker, dev, or production walk.
Base: pykbroker 5ce2bc4ff14ea66ddf49a8c521cb54db01d85141.
Official reference: DBsecurities/dbsec-open-api commit 564b5100b80a3a785468b77ee8c0557f6edcfdeb.

- Reused existing `Pydb.DBFI.get_able_order_quantity`; no new endpoint, client, dependency or credential.
- KR CSPBQ00100 returns `MgnRat100pctOrdAbleAmt` (100% margin buying power).
- CSPAQ03420 `Dps2` is D+2 deposit and is no longer treated as buying power.
- US CAZCQ01300 uses native currency (`won_fcurr_type="2"`).
- `is_integrated=False` uses `AstkOrdAbleAmt`; explicit True uses `AstkOrdAbleAmt0` only when `CsldtMgnUseYn="Y"`.
- `AstkOrdAbleAmt1` (includes debit) is never selected. No local FX/equity/deposit conversion.
- `available_cash(symbol=..., price=...)` supports the actual intended order. No-argument US snapshot retains the existing Pydb.services AAPL/1 query; it is not a universal per-symbol quantity quote.
- Constructor flag must be a bool; the trader factory must parse and pass the account's explicit `is_integrated` flag.
- An explicitly injected `cash_provider` retains its existing caller-owned policy.
- KR CSPAQ04800 average price comes from `AvrExecPrc` only with nonzero executions.
- US CAZCQ00100 explicitly requests aggregate rows (`query_type="0"`) and native currency; average comes from USD `AstkExecAmt` / `AstkExecQty`.
- `OrdQty` / `AstkOrdQty` is carried through. Zero remaining proves filled only when executed equals original positive order quantity.
- Missing prices remain None; the submitted limit price is never used. Invalid or contradictory quantities fail closed.
- Existing submission, cancel, safety, idempotency, rules, and token-preserving close are reused unchanged.
- Trader needs `fill_price` in its `_average_fill_price` field aliases; SDK model already defines it.
- DB US live-gate and Q route changes remain the central integration owner's responsibility. No gate enabled here.
- Mock DB endpoint support is not implemented; this change keeps `supports_virtual=False`.
- Offline regressions: DB contract, US contract, available funds, DB close, contract uniformity, execution contract.

Official source links:
- https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/examples/kr_stock_order/kr_stock_inquire_psbl_quantity.py
- https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/examples/kr_stock_order/kr_stock_inquire_balance.py
- https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/examples/ov_stock_order/ov_stock_inquire_psbl_amount.py
- https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/examples/kr_stock_order/kr_stock_inquire_executions.py
- https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/examples/ov_stock_order/ov_stock_inquire_executions.py
