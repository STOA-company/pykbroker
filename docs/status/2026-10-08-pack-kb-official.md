# KB official response correction — 2026-10-08
- Scope: KB adapter only; integration base a7bf2a84562073807118ab06b2bd83c122a1bb2c.
- Result: partial; SPQM2226 remains incomplete; 10-24 KR limit amendment contract added.
- Customer scenes: ambiguous/nonempty KR holdings fail explicitly; US nonempty evidence cannot become empty.
- SKQM3350: count active currency rows; zero/blank fixed-array padding excluded (6 active of 10).
- Funding rejects missing active rows, nonzero unidentified padding, malformed rows and duplicate USD.
- SPQM2226: count 1, Record2 empty, positive securities valuation, displaced TSLA fields in Record1.
- Empty US Record2: reject nonzero count, positive valuation totals or stock evidence in Record1, even with count 0.
- XLSX labels grid_cnt/grid_cnt2 only; cumulative/page semantics have no prose definition.
- SPQM2226 normal synthetic pages require exact per-page count and bounded unique nxt_key continuation.
- cn_f enum is undocumented; no invented continuation flag.
- SSQM2952 sheet42:51-55,68-72 labels ten quantity fields; no calculation formula is documented.
- Any nonzero quantity/P6 or positive row valuation raises SpecUnverifiedError; count/truncation checks preserved.
- KR request price: positive whole won, fixed integer string, String(9).
- US request price: positive P4, at most four significant fractional places, String(15).
- Strip insignificant decimal zeroes without rounding; reject excess precision/length before transport.
- US ledger retains frgn_ordr_prc_p6; cancel serializes its exact value as P4, trimming only insignificant zeros.
- US order-date timezone undocumented; existing New York default and explicit order_date preserved.
- KR cancel: crct_clsf=1 + quantity per successful SSAM1806 JSON.
- Conflict: sheet28:13 says 1 partial correction / 2 full correction; no inference of value 2.
- KR order/cancel symbols normalized before serialization and identity checks.
- All five write TRs validate every serialized field length at final transport boundary; no truncation.
- issue_token: explicit Client Credentials; dataHeader/dataBody per token.json and official auth_example.py.
- Returns issued access_token/token_type/expires_in only; no secret cache/log, auto-issue or renewal.
- Token host, inputs, business envelope, token/expiry checked; errors raised outside except sever context/cause.
- Writes remain off by default; OrderSafety and factory registration unchanged; other brokers untouched.
- Sources: tests/fixtures/kb/official/SOURCE.md; original ZIP/XLSX hashes verified; no new downloads.
- Fixtures: ten detached official examples with fake identities and original/sanitized SHA256.
- Tests: tests/test_kb_official_contract.py; official replay, serialization, boundaries, corrupt responses.
- Baseline: 1318 passed, 5 expected failures, 19 skipped (46.35s); no external calls.
- Fix1: 1406 passed, same 5 expected failures, 19 skipped (36.40s); KB 88 passed; new failures zero.
- Scope excludes fractional holdings, market/modify/reserve orders, consumer storage and default switches.
- Live only: KR quantity semantics; SPQM2226 native rows/currency, >15 holdings continuation, date/fractional price.
- Live only: token/account binding, KR quantity-specific cancellation of full remainder, acknowledgements vs ledger.
- No mock API. First live call: independent review + CEO approval, one real-account read only.
- Then approved one-share limit order/cancel reconciliation; no live order authorization in this PR.
- Draft PR + do-not-merge only; no merge, deployment, VM/DB/queue access or independent review invocation.

## 2026-10-24 domestic contract delta
- Contract/source/unknowns: tests/fixtures/kb/official/CONTRACT-2026-10-24.md and SOURCE.md.
- KB-only SSAM1805 quantity-specific amendment uses existing safety/dedupe; ACK side unknown.
- Stateful fake + real REST JSON-byte replay cover limit buy/sell, partial fill, amend/cancel/ledger, explicit token reissue.
- Market price sentinel, nonempty KR holdings, native error placement and numeric quota remain unverified; scope partial.

## 2026-10-25 재검수 fix2
- KR 능력 order_kinds=(limit,), supports_market_order=False로 공식 계약과 일치.
- 미확인 가격값의 시장가·최유리·최우선·중간가 등 미선언 종류는 공용 검증에서 HTTP 전 거절.
- 공식 코드표는 지원 선언과 별개로 보존; 지정가 KRX/NXT/SOR·정정·취소 계약 유지.
- tests/test_kb_contract_walk.py는 매수·매도 7종 미선언 주문 전송 0 단언; 실키·실주문 0.
