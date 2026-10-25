# Kiwoom REST spec table (domestic stock, as implemented in `pykbroker.brokers.kiwoom`)

Source: official Kiwoom spec JSON, repo `Kiwoom-Securities/Kiwoom-REST-API`, file `kiwoom/_data/kiwoom_api_spec.json`,
sha256 `42a7b3912c9d9588c83bdc2db7779c8d2e038703a2b5562e54ef46ae905cba79` (same bytes as the 2026-10-08 pin in
`tests/fixtures/kiwoom/official/SOURCE.md`). Re-fetched from `main` on 2026-10-25 16:24 KST: identical sha256, so the
spec did not change since the pin (upstream HEAD `953e5dbf`, 2026-09-15). The portal pages
(`openapi.kiwoom.com/guide/...`) render client-side and could not be fetched by tool; rate-limit and token prose below
is the portal reading recorded in `src/pykbroker/brokers/kiwoom.py` (2026-09-26 12:44 KST) and is marked as such.

Machine-readable extract used by the contract server: `tests/fixtures/kiwoom/official/request_schema.json`,
full error table: `tests/fixtures/kiwoom/official/error_codes_all.json` (37 rows).

## Hosts, headers, token

| Item | Spec | Adapter |
|---|---|---|
| Real host | `https://api.kiwoom.com` (every TR page) | `KIWOOM_API_BASE_URL` |
| Mock host | `https://mockapi.kiwoom.com` (every TR page) | `KIWOOM_MOCK_BASE_URL`, chosen only by `virtual=True` |
| Method / format | POST, `application/json;charset=UTF-8` | same |
| TR headers | `api-id` (Y), `authorization` = `Bearer <token>` (Y), `cont-yn`, `next-key` (continuation) | same; continuation followed to the end or refused |
| Token issue `au10001` | POST `/oauth2/token`, body `grant_type=client_credentials`, `appkey`, `secretkey`; response `token`, `token_type`, `expires_dt` (14 digits) | `Kiwoom.issue_token`, explicit call only; no header table on that page, no `api-id` sent |
| Token revoke `au10002` | body `appkey`, `secretkey`, `token` | not used |
| Token lifetime / zone of `expires_dt` | not stated in the spec JSON | read as KST, refused 5 min early (portal reading) |
| Rate limit | real 5 order + 5 read calls/s, mock 1 call/TR/s (portal reading); errors 1700 (per API), 1701 (total), 1702 (group) | per-TR limiter `KIWOOM_TR_RATE_LIMITS`; 1700-1702 raise `BrokerRejectedError`, no auto-retry |
| Key mode mismatch | 8030 (appkey real/mock differs), 8031 (token real/mock differs); 8104 API not supported in mock | surfaced as refusal |

## TRs used

| TR | Name | URL | Required request fields | Optional |
|---|---|---|---|---|
| `au10001` | 접근토큰 발급 | `/oauth2/token` | appkey, grant_type, secretkey | - |
| `ka10001` | 주식기본정보요청 | `/api/dostk/stkinfo` | stk_cd | - |
| `ka10004` | 주식호가요청 | `/api/dostk/mrkcond` | stk_cd | - |
| `kt00001` | 예수금상세현황요청 | `/api/dostk/acnt` | qry_tp | - |
| `kt00011` | 증거금율별주문가능수량조회요청 | `/api/dostk/acnt` | stk_cd | uv |
| `kt00018` | 계좌평가잔고내역요청 | `/api/dostk/acnt` | dmst_stex_tp, qry_tp | - |
| `ka10075` | 미체결요청 | `/api/dostk/acnt` | all_stk_tp, stex_tp, trde_tp | stk_cd |
| `kt00007` | 계좌별주문체결내역상세요청 | `/api/dostk/acnt` | dmst_stex_tp, qry_tp, sell_tp, stk_bond_tp | fr_ord_no, ord_dt, stk_cd |
| `kt10000` | 주식 매수주문 | `/api/dostk/ordr` | dmst_stex_tp, ord_qty, stk_cd, trde_tp | cond_uv, ord_uv |
| `kt10001` | 주식 매도주문 | `/api/dostk/ordr` | dmst_stex_tp, ord_qty, stk_cd, trde_tp | cond_uv, ord_uv |
| `kt10003` | 주식 취소주문 | `/api/dostk/ordr` | cncl_qty, dmst_stex_tp, orig_ord_no, stk_cd | - |

Order fields: `dmst_stex_tp` KRX|NXT|SOR (adapter sends KRX only), `trde_tp` 0 limit / 3 market / 5 conditional limit /
81, 61, 62 after-hours (adapter sends 0 only), `ord_no` is 7 digits, `cncl_qty` `0` cancels the whole remainder
(adapter always sends the explicit quantity).

## Errors (official table, selected)

1501/1504/1505 api-id missing or wrong for URI; 1511 required field missing; 1512-1516 header/authorization form;
1517 value format; 1700-1702 rate; 1901-1903 unknown symbol; 1999 unexpected (on a write the adapter treats it as
unknown, never as safe to resubmit); 8001-8003, 8005 (token invalid), 8010 (token IP differs from caller IP), 8011-8012,
8020, 8030, 8031, 8040, 8050, 8103, 8104, 8200 (corporate customers unsupported). Order business errors (insufficient
cash, bad order number) are not in this table.

## Not confirmed (kept fail-closed or unknown)

1. Where an error arrives: HTTP status vs body `return_code`. Adapter: HTTP failure = transport (unknown for a write),
   body code != 0 = refusal. The contract server answers in the body.
2. Token lifetime and `expires_dt` time zone. 3. Whether the rate bucket is per TR or per account.
4. Order business error codes (spec gives none); the contract server uses code 20 as a stand-in, adapter treats every
   non-zero integer as refusal.
5. kt00007/ka10075 row text after cancel or partial fill (`mdfy_cncl` "취소확인", `cnfm_qty`) is only shown in the
   official examples without a cancel case; the adapter reads a cancel only when the linked confirmed quantities add up.
6. Market orders, NXT/SOR routing, after-hours order types, amend (kt10002): not implemented (limit KRX only).
7. `kt00001` qry_tp meaning (2 vs 3) and kt00018 `qry_tp`: adapter uses the one that cannot overstate cash.
8. Real mock round trip against `mockapi.kiwoom.com`: not run (no mock key on this machine).
