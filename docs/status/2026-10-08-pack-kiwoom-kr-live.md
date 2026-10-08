# 키움 국내 실주문 어댑터 팩 (2026-10-08)
- 기준: a7bf2a84562073807118ab06b2bd83c122a1bb2c; worktree codex/kiwoom-kr-live-1008.
- 위치: mwork2; 오프라인 어댑터 변경·draft PR만. 증권사 서버 호출 0건, VM·DB 접근 0건.
- 공식 출처: https://raw.githubusercontent.com/Kiwoom-Securities/Kiwoom-REST-API/69642586f7d84ba9fd8a6faf1f1537c7fda6568b/kiwoom/_data/kiwoom_api_spec.json
- 받은 시각 2026-10-08 09:07:51 KST; 원본 sha256 42a7b3912c9d9588c83bdc2db7779c8d2e038703a2b5562e54ef46ae905cba79.
- 요청·응답 예시는 tests/fixtures/kiwoom/official/; 계좌 칸은 가짜 값, kt10000 누락 쉼표만 복원.
## 고친 것·동일 종류 범위
- A1: 가격 문자열 부호를 방향으로 읽어 절댓값. ka10001 연중최저 -91200·ka10095 하한가 -85200 근거.
- 대상: 현재가·매수/매도호가·상/하한가·기준가·매입/잔고현재가·미체결 주문가·체결단가.
- 가격 밴드 모순 거절 유지. 수량은 절댓값 변환 없음. 원본 ka10001 어긋난 값은 계속 거절.
- A4: kt00001 ord_alow_amt·entr·100stk_ord_alow_amt·d2_entra 최솟값, 음수 후보는 0원.
- 공식 예시 85,341 주문가능·17,534 예수금·12,550 100%/D+2 → 현금 12,550원.
- 기존 orderable_quantity에 KR kt00011(min_ord_alowq) 분기 추가, 양수 정수 원 가격만 조회.
- KR 조회 5/s 유지: kt00001을 0.5/s로, 신규 kt00011도 0.5/s. US 예산/동작 유지.
- A5: kt00007 취소확인의 ori_ord·종목·KRX 대조, cnfm_qty 합산; 주문=체결+취소+잔량일 때 판정.
- 확인취소>0·잔량0은 cancelled. 불일치·중복·다른 종목·취소접수는 unknown; 정정의 기존 완전체결 유지.
- ka10075 평균 체결가: 양수 cntr_tot_amt / cntr_qty만 채움. 누락·0이면 None.
- A7: experimental.kiwoom.get_balance 호출 전 오류; 추정예탁자산(prsm_dpst_aset_amt) 현금 반환 제거.
- README·한/영 BROKER_MATRIX·모의 예제의 가격 부호 설명 동기화.
- A8 중단: 1700~1702 유량 초과는 공식 오류표 확인; 기존 retryable 분류 없음, BrokerRejectedError 유지.
- A3·A11: 시작 HEAD에 venue·is_integrated·미국 구현이 이미 있음. 추가 없음.
- #19 대조: 빠진 국내 동작 없음. 옛 fc_ord_alowa 현금·US 정지 확인 제거는 범위 밖/방어 후퇴라 제외.
## 검증
- 명령: PYTHONPATH=src /home/quantus/coding/pykbroker/.venv/bin/python -m pytest -q tests -p no:cacheprovider
- 기준선: 1318 passed / 19 skipped / 5 failed; 끝: 1354 passed / 19 skipped / 같은 5 failed.
- 기존 실패: contract_uniformity factory 목록·US-NH, dbfi 현금, locks LS flag, rules_contract KIS US 규칙.
- 키움 4파일: 283 passed. 변경을 되돌린 회귀 확인: 12 failed / 6 passed(의도된 실패); 수정 파일 복원.
- 변경 Python 파일 ruff check·git diff --check 통과. pytest 5/8회, no_network 소켓 차단.
## 모의·실계좌에서만 확인할 것 (안 걸어 봄)
- A2: 모의 왕복·하락 종목 raw 부호·HTTP 오류 위치. 실제 호출 금지여서 시행 안 함.
- A4: qry_tp=2 현금·오늘 매수 정산·kt00011 종목/가격별 수량을 실계좌로 대사.
- A5: 취소확인 ori_ord·cnfm_qty 실제 줄 모양·분할체결 총액. 공식 예시에 없어 파생 시험임.
- 카드→확인→접수→취소 고객 UI는 안 걸어 봄. 독립 리뷰·재검수·대표 승인 뒤 main 머지.
- 시장가·NXT/SOR·웹소켓·자동 토큰 재발급·allow_live_orders·OrderSafety·factory 변경 없음.
