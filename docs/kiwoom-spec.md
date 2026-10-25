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

Order fields: `dmst_stex_tp` KRX|NXT|SOR, `trde_tp` see the order-kind table, `ord_no` is 7 digits, `cncl_qty` and
`mdfy_qty` `0` mean the whole remainder (adapter always sends the explicit quantity), kt10002 adds `mdfy_uv` (required)
and `mdfy_cond_uv`.

## Errors (official table, selected)

1501/1504/1505 api-id missing or wrong for URI; 1511 required field missing; 1512-1516 header/authorization form;
1517 value format; 1700-1702 rate; 1901-1903 unknown symbol; 1999 unexpected (on a write the adapter treats it as
unknown, never as safe to resubmit); 8001-8003, 8005 (token invalid), 8010 (token IP differs from caller IP), 8011-8012,
8020, 8030, 8031, 8040, 8050, 8103, 8104, 8200 (corporate customers unsupported). Order business errors (insufficient
cash, bad order number) are not in this table.

## Not confirmed (kept fail-closed or unknown)

1. Where an error arrives: HTTP status vs body `return_code`. Adapter: HTTP failure = transport (unknown for a write),
   only documented rejection codes prove refusal; unlisted codes stay unknown. The contract server answers in the body.
2. Token lifetime and `expires_dt` time zone. 3. Whether the rate bucket is per TR or per account.
4. Order business error codes (spec gives none); the contract server uses code 20 as a stand-in, adapter keeps this unlisted synthetic code unknown.
5. kt00007/ka10075 row text after cancel or partial fill (`mdfy_cncl` "취소확인", `cnfm_qty`) is only shown in the
   official examples without a cancel case; the adapter reads a cancel only when the linked confirmed quantities add up.
6. `cond_uv` / `mdfy_cond_uv` meaning for trde_tp 28 스톱지정가. Searched 2026-10-25: official spec JSON, the official GitHub
   repo (examples, postman collection), the pinned error table. Evidence is only indirect (query responses label the same
   field «스톱가»); no order page says it is the trigger price, whether it is required for 28, or what `mdfy_cond_uv` means.
   The slot is built and shut. **Inquiry to Kiwoom (1 message, verbatim):** «kt10000/kt10001 의 trde_tp=28(스톱지정가)에서
   cond_uv(조건단가)가 스톱(트리거) 가격입니까? ord_uv는 트리거 후 지정가입니까? 28에서 cond_uv 필수 여부, 매수/매도별 트리거
   방향(이상/이하), 모의투자 지원 여부, 정정(kt10002)의 mdfy_cond_uv 의미와 28 주문의 정정·취소 가능 여부를 알려 주세요.»
   Who/when: the account holder with the Kiwoom API portal login (대표/운영) sends it via the portal Q&A; needed before the
   flag is turned on. Other open items: Whether 61/81 take
   a price (sent empty) and 62 does (sent when given). NXT/SOR holdings under kt00018 (queried as KRX). `ka10001/04` exchange
   suffix for quotes (`_NX`, `_AL`): quotes stay KRX.
7. `kt00001` qry_tp meaning (2 vs 3) and kt00018 `qry_tp`: adapter uses the one that cannot overstate cash.
8. Real mock round trip against `mockapi.kiwoom.com`: not run (no mock key on this machine).

## Order-kind table (same shape for every broker)

Columns: kind as the caller states it (`order_type` / `time_in_force` / `session` / `venue`) -> broker wire value ->
spec evidence -> contract-server walk -> real mock walk. «walk» = a multi-turn test in `tests/test_kiwoom_order_kinds.py`
or `tests/test_kiwoom_contract_server.py` through the production adapter. «mock» = `mockapi.kiwoom.com` (KRX only per
the official mock info, 2026-10-24); none has been walked there (no mock key).

| Kind | Caller states | Wire | Spec evidence | Supported | Contract walk | Real mock walk |
|---|---|---|---|---|---|---|
| Limit | `limit` + price | kt10000/1 `trde_tp=0` | trde_tp list «0:보통» | yes | yes | not run |
| Market | `market` | `3` | «3:시장가» | yes (needs session authorization) | yes (fills at quote) | not run |
| Conditional limit | `conditional_limit` + price | `5` | «5:조건부지정가» | yes | yes (sent) | not run |
| Best / top limit | `best` / `priority` | `6` / `7` | «6:최유리지정가, 7:최우선지정가» | yes | yes | not run |
| Mid price | `midpoint` | `29` | «29:중간가» | yes | yes | not run |
| IOC | `time_in_force=ioc` on limit/market/best/midpoint | `10/13/16/30` | «10:보통(IOC) 13 16 30» | yes | yes (remainder killed) | not run |
| FOK | `time_in_force=fok` on limit/market/best/midpoint | `20/23/26/31` | «20:보통(FOK) 23 26 31» | yes | yes (all or nothing) | not run |
| Stop limit | `stop_limit` + price + `stop_price` | `28`, trigger in `cond_uv` | «28:스톱지정가» listed; `cond_uv` is «조건단가 (단위: 원)» on the order pages, called «스톱가» only in query responses (kt00007, kt00009, kt50030/31/75) and `stop_pric` «스톱지정가주문 스톱가» in ka10075/ka10076 | **slot built, shut** (`KIWOOM_STOP_LIMIT_VERIFIED=False`; not in `order_kinds`) | yes: refused before the wire; with the flag on, 28 + `cond_uv` is built | - |
| After-hours close | `close` | `81` | «81:장마감후시간외»; same meaning as KIS/DB `close` | yes | yes | not run |
| Pre-open session | `market` + `session=pre` | `61` | «61:장시작전시간외» | yes | yes | not run |
| Single-price session | `limit`/`market` + `session=single_price` | `62` | «62:시간외단일가» (price rule unstated) | yes | yes | not run |
| NXT | `venue=NXT` | `dmst_stex_tp=NXT` | «KRX,NXT,SOR»; ka10075 `stex_tp` 0/1/2, kt00007 `%/KRX/NXT/SOR` | yes (real host only) | yes (all kinds above) | impossible: mock is KRX only |
| SOR | `venue=SOR` | `dmst_stex_tp=SOR` | same | yes (real host only) | yes | impossible: mock is KRX only |
| Amend (native) | `amend_order(AmendRequest)` | kt10002 | kt10002 page: `mdfy_qty`, `mdfy_uv`; new `ord_no` | yes, KR limit original only | yes (partial fill then amend) | not run |
| Cancel | `cancel_order(CancelRequest)` | kt10003 | kt10003 page | yes, venue must match the order | yes | not run |
| US | plain day limit | usa/ust TRs | see 2026-10-08 pack | limit only | existing tests | not run |

`amend_no_double_fill` (native amend TR): the amend request is sent once under its own idempotency key. A timeout or
unreadable answer is `TransportError`/`InvalidResponseError` (unknown), the same key is refused (`duplicate`) and no
second kt10002 or new order is made until the caller reads `list_orders`/`status`. After a partial fill the amend
quantity may not exceed the remainder (server refusal, nothing created). Walk: `test_amend_unknown_answer_is_not_resent...`,
`test_amend_after_partial_fill_never_exceeds_the_remainder`. A «cancel + new order» form is not used by this adapter.

Adapter choices that differ from before this table (all data cells, no broker-name branching in shared code):
`list_orders` asks `stex_tp=0` (통합) on the real host and `1` (KRX) on the mock; `status` asks `dmst_stex_tp` of the
order's venue (KRX when unknown); cancel/amend answers must echo the requested venue.

## SEAM cross-check (SEAM.md of 2026-10-25, base `integ/q-order-four-brokers-1024` 4e2860c)

| SEAM cell | This change | Match |
|---|---|---|
| §3 amend = `amend_order(AmendRequest)` | Kiwoom keeps the integ method, not duplicated | match |
| §3 `Capabilities.supports_amend` | added (default False); Kiwoom KR True | match |
| §7 I2 `Order.client_key` echo | added (optional, hidden from repr); Kiwoom place/amend/cancel fill it; other brokers still None | partial: only Kiwoom fills it (KB/LS/NH/KIS/DB/Toss are other workers' share) |
| §4 reject classes as data rows with source | `DEFINITIVE_REJECTS["kiwoom"]` = 36 rows of the official error table, 1999 excluded (unknown) | match; business errors (insufficient cash) are not in the table so they stay a plain refusal |
| §5 `us_class_separator` + `canonical_symbol` | BaseBroker `"."`, KIS `"/"`, inverse added | partial: responses of KIS are not yet rewired to `canonical_symbol` |
| §2 B `OrderRequest` fields | unchanged shape; added `time_in_force`, `session` (defaults keep every old call identical); `order_type` gained `conditional_limit/best/priority/midpoint` | different on purpose: A (`limit·market`) cannot yet express these; the trader's map must pass them through |
| §2 `venue` for NXT | existing `venue`/`supports_nxt` cells, SOR added to `venues` | match |
| Capability support lists (S-추가) | `Capabilities.order_kinds`, `time_in_force`, `sessions`, `venues` as data; empty ones derive from the flags (limit/market, day, regular). Kiwoom KR: 7 kinds (+stop_limit when verified), day/ioc/fok, regular/pre/single, KRX/NXT/SOR (mock KRX); KIS and DB KR: limit/market/close; US of every broker and every other broker: what the flags already said. The trader's capability response is not wired (D1) | match |
| §8 item 5 contract mock for Kiwoom | `tests/kiwoom_contract_server.py` | match |
| D2 KB/LS amend rename | not touched | out of scope |

## Tests and CI

The repo has no CI workflow (no `.github/`), so nothing runs on the PR automatically. Run the suite as
`PYTHONPATH=src:<toss-openapi checkout>/src python -m pytest -q tests` in a venv with the `dev` extra plus
`pydantic>=2,<3` (pyproject `dev` and `toss` extras are the only installs; no dependency was added).
`toss_openapi` is a vendor source tree with no published distribution (pyproject comment and docs/notice.md), so the
documented way is putting that checkout on the import path (docs/toss-b-resource-pack.md:13 uses
`/home/quantus/coding/toss-openapi/src`, commit 53f9950). Without it `test_real_sdk_refuses_a_decimal_quantity_buy...`
fails on import; with it the whole suite is green.
