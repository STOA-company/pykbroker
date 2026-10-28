# 국내 잔고 orderable_cash 계약 — 2026-10-28

- 범위: SDK `Balance.orderable_cash: Decimal | None = None` 선택 칸과 trader 문자열/null 전달. 주문·취소 수정 0.
- SDK 기준 HEAD `609931c6d0cd19f019694fd8718f47f131af87ca`; 커밋·push·실제 증권사 호출·운영/VM/배포 0.
- 정식 증권사 7개: `src/pykbroker/factory.py:17`. 아래 경로는 `src/pykbroker/brokers/` 기준.

| 증권사 | orderable_cash 근거 | 기존 cash 의미 |
|---|---|---|
| 한투 | `kis.py:351`, `ord_psbl_cash` 그대로 | 주문가능 현금 |
| 토스 | `toss.py:426`, KRW `cashBuyingPower` 그대로 | 현금 구매력 |
| DB | `dbfi.py:187`→기존 `available_cash`, CSPBQ00100 `Out.MgnRat100pctOrdAbleAmt`(:157) | `Dps2` D+2 예수금은 :183에 보존 |
| LS | `ls.py:517`, CSPAQ12200 `MnyOrdAbleAmt` 그대로 | 현금 주문가능액 |
| NH | None (`nh.py:593`~602) | `min(dca, orr_pbl_amt4, orr_pbl_amt)` 기존 보수 산식; 원시 주문가능액 자체가 아님 |
| 키움 | None (`kiwoom.py:380`~394) | kt00001 `min(ord_alow_amt, entr, 100stk_ord_alow_amt, d2_entra)` 기존 예수금/결제 상한 |
| KB | `kb.py:534`, SSQM0004 `ordr_psbl_csh` 그대로 | 주문가능 현금 |

- DB 근거 정본: `2026-10-04-pack-dbfi-cash-fills.md:8`; 추측 TR·필드 추가 0.
- DB KR 추가 읽기 1회: 기존 기본 `stock_code=005930`, `order_type=2`, `price=0`; 특정 주문의 수량 보장은 아님.
- DB 성공코드/필드 누락은 InvalidResponseError, 오류 코드는 BrokerRejectedError로 잔고 조회 전체 실패; 예수금 대체 0.
- DB caller cash_provider는 출처 보장 안 되어 새 칸 None. 기존 available_cash 정책은 보존.
- US 새 칸 None; 기존 US 현금 산식/가용금·pilot funding 동작 변경 0.
- trader `account_safety_apis.py:245` 변환, :254 `_balance_payload`, :282 `_sdk_balance_payload`만 변경.
- trader `get_account_balance` 전체 AST는 기준과 같음(메인 확인); cash/nav/positions/prices 기존 값 보존.
- 핀은 trader `pykbroker` gitlink; `.gitmodules:10`, `requirements.txt:60`~64, `dockerfile:25`의 로컬 설치.
- 현재 trader gitlink `bee3bac63a838b9bb774004509bac4ff18f0fcfa`; 새 SDK 커밋 후 창이 교체, 이 작업 핀 변경 0.
- DB 시험은 기존 client response mock 경유이며 HTTP 가짜 서버 수준이 아님. 성공/누락/오류와 주문·취소 0을 확인.
- 바뀐 계약 시험 678 passed. SDK 소켓 없는 묶음 1882 passed/24 skipped/2 deselected; HTTP 파일 31개 별도 제외.
- SDK 원명령은 sandbox socket 금지 32개 + 미설치 toss SDK 1개로 33 failed; 전체 초록 주장 없음.
- trader account_safety 단독 64 passed; 정본 pycex 핀 환경의 지정 선택 89 passed/5396 deselected. 양쪽 bandit -ll medium/high 0.
- 독립 Opus 검수는 API DNS EAI_AGAIN으로 미실행; 판정 아직 아니다. 상세/원명령은 루트 `REPORT-orderable-cash.md`.
- 원본 로그 `/tmp/orderable-cash-1028-worker/`; 네트워크 차단 가드 변경 0. 고객/dev/실제 계좌 경로 안 걸어 봄.
