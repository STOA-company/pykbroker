# 키움 국내 현금 주문 계약 모의 — 2026-10-24 확인

이 표와 `test_kiwoom_stateful_contract.py`는 공식 문서의 필드 계약을 검증한다. 가짜 서버의 체결·잔고·원장 전이는 합성 시나리오이며 키움 서버를 실행하거나 실계좌를 검증한 결과가 아니다. 실제 factory의 `brokers.Kiwoom` 경로를 쓴다. `experimental.KIWOOM_SPEC_VERIFIED=False`는 experimental 경로만 막으며 등록 어댑터의 실주문 잠금이 아니다. 등록 어댑터의 기본 잠금은 `OrderSafety(dry_run=True)`이고, 트레이더의 별도 잠금은 `LOCKED_BROKERS`·`live_gate(stock_account_live_unsealed)`다. 이 계약 모의는 기본 dry-run에서 주문·정정·취소 전송이 0임을 검증하며 트레이더 잠금의 실행 검증은 포함하지 않는다.

## 출처

- [공식 포털](https://openapi.kiwoom.com/main/home)에서 공식 GitHub·JSON 명세 제공을 확인.
- [공식 JSON 고정본](https://raw.githubusercontent.com/Kiwoom-Securities/Kiwoom-REST-API/69642586f7d84ba9fd8a6faf1f1537c7fda6568b/kiwoom/_data/kiwoom_api_spec.json), 2026-10-24 재확인. main 원본 SHA256 `42a7b3912c9d9588c83bdc2db7779c8d2e038703a2b5562e54ef46ae905cba79`가 기존 고정본과 일치. `official/kt10002.json`은 원본 요청·응답 그대로, `official/contract_errors.json`은 관련 오류표 행 그대로 추출.
- [공식 이용안내](https://openapi.kiwoom.com/intro/serviceInfo) §07: 접근토큰 유효기간 24시간, 24시간마다 재발급. 확인일 2026-10-24.
- [공식 소개·호출 제한](https://openapi.kiwoom.com/intro): 국내 계좌별(토큰별) 주문 5회/초, 조회 5회/초; 모의 계좌별(토큰별) TR별 1회/초. 확인일 2026-10-24.
- [공식 모의 안내](https://openapi.kiwoom.com/intro/mockInvestInfo): 국내 모의 매매·계좌조회 KRX만. 확인일 2026-10-24.

## 필드 계약 (모두 POST, JSON)

| 목적 / api-id | 엔드포인트 | 요청 | 응답·판정 |
| --- | --- | --- | --- |
| 토큰 au10001 | /oauth2/token | grant_type=client_credentials, appkey, secretkey | token, token_type=bearer, expires_dt(예시 14자리), return_code=0 |
| 매수 kt10000 / 매도 kt10001 | /api/dostk/ordr | dmst_stex_tp=KRX, stk_cd, ord_qty, trde_tp=0(지정가)/3(시장가), ord_uv=원 정수/빈 문자열 | ord_no; 접수는 체결 아님 |
| 정정 kt10002 | /api/dostk/ordr | dmst_stex_tp, orig_ord_no, stk_cd, mdfy_qty, mdfy_uv; mdfy_cond_uv 선택 | ord_no=새 주문번호, base_orig_ord_no=모주문, mdfy_qty, dmst_stex_tp(선택; 생략=KRX). 양수 지정 수량만 구현. ka10075에서 원주문이 보통/지정가로 확인된 경우만 가격 정정 |
| 취소 kt10003 | /api/dostk/ordr | dmst_stex_tp, orig_ord_no, stk_cd, cncl_qty | 새 ord_no, base_orig_ord_no, cncl_qty. 요청 수량 일치 확인; 원주문 상태는 원장 조회 |
| 예수금 kt00001 | /api/dostk/acnt | qry_tp=2 | ord_alow_amt, entr, 100stk_ord_alow_amt, d2_entra의 최소 현금 |
| 보유 kt00018 | /api/dostk/acnt | dmst_stex_tp=KRX, qry_tp=1 | acnt_evlt_remn_indv_tot: stk_cd, stk_nm, rmnd_qty, trde_able_qty, pur_pric, cur_prc, evlt_amt |
| 미체결 ka10075 | /api/dostk/acnt | all_stk_tp, trde_tp=0, stex_tp=1; stk_cd 선택 | oso: ord_no, ord_qty, cntr_qty, oso_qty, cntr_tot_amt; 부분 체결·평균가 |
| 당일 체결·상태 kt00007 | /api/dostk/acnt | ord_dt, qry_tp=1, stk_bond_tp=1, sell_tp=0, stk_cd, dmst_stex_tp=KRX | acnt_ord_cntr_prps_dtl: ord_no, ori_ord, ord_qty, cntr_qty, ord_remnq, cnfm_qty, mdfy_cncl |

목록 응답의 cont-yn=N만 끝으로 인정하며 Y에는 next-key 연속 조회를 사용한다. 필드의 전체 원문은 기존 공식 fixture와 SOURCE.md에 연결된다. 현금/수량은 부호를 임의로 바꾸지 않으며 가격의 방향 부호만 크기로 읽는다.

## 오류·중복·수명·한도

| 계약 | 근거와 처리 |
| --- | --- |
| 1511 | 필수 입력 누락 → BrokerRejectedError, 재전송 0 |
| 1902 | 종목 정보 없음 → BrokerRejectedError, 재전송 0 |
| 8005 | 유효하지 않은 토큰 → BrokerRejectedError, 자동 발급·주문 재전송 0 |
| 1700 / 1701 / 1702 | API별 / 전체 / 그룹 유량 초과 → BrokerRejectedError, 재전송 0 |
| 1999 | 예상 밖 오류 → 쓰기 결과 unknown(InvalidResponseError), 같은 client_key 재전송 거절 |
| 토큰 | 공식 유효기간 24시간. 응답 expires_dt를 사용하고 기존 5분 조기 만료 판단 유지. 호출자가 명시 재발급 후 broker 재생성; 비밀값 저장·자동 재시도 없음 |
| 한도 | 기존 TR별 1회/초 이하 배분 유지, 정정 kt10002=1회/초 추가. 국내 쓰기 4 TR 합계 4회/초, 조회 합계 5회/초 이하 |
| 중복 | 공식 서버 client key/idempotency 지원 미확인. 기존 SDK OrderSafety의 broker/account/action/client_key 저장소로 같은 place/amend/cancel 각각 한 번만 전송. 다른 작업 action과는 구별. 여러 프로세스 보장은 주입한 저장소 책임 |

## 미확인 칸·보수적 경계

- expires_dt 시간대는 미확인: 기존 KST 해석·5분 여유 유지. 재발급이 다른 프로세스의 토큰에 미치는 영향 미확인.
- 실패가 HTTP 오류인지 JSON return_code인지, 초과 뒤 재개 시각·Retry-After·분/시간 추가 한도·실제 윈도 경계는 미확인. JSON 오류와 전송 실패를 각각 시험하며 자동 재전송하지 않음.
- 정정·취소 후 실제 원장의 행 결합·전이 시점은 미확인. 모주문 번호는 즉전 주문번호와 다를 수 있음. 정정 접수 응답은 새 id와 accepted만, side/type 확정 없음. 변경 전 주문의 정정 연결 원장은 기존 unknown 판정 유지.
- 정정 요청에는 원주문 유형이 없고 어댑터는 주문 캐시를 보관하지 않으므로, 기존 안전장치 통과 후 `list_orders(symbol)`로 원주문 유형을 한 번 조회한다(연속 페이지는 기존 조회 계약). 시장가·미확인 유형·조회에 없는 원주문은 `NotSupportedError`로 거절하며 kt10002를 보내지 않는다. 조회 실패에도 정정을 보내지 않고 같은 client_key를 재전송하지 않는다.
- 실제 부분 체결·수수료·세금·미결제 정산은 미확인. 가짜 서버는 무수수료·즉시 현금 반영을 시험 편의로만 사용하며 공식 서버 동작으로 주장하지 않음.
- 시장가 주문의 실계좌 허용을 별도로 열지 않음. 기존 명시 세션 허용·보수적 원화 평가·dry-run·중복·중지 장치를 통과해야 함.

## 계약 모의 통과 범위

시험은 시장가/지정가 매수·매도, 부분 체결→잔량 정정→취소→원장/보유/잔고 대사, 토큰 발급→만료 차단→명시 재발급, 유량 초과 3코드와 오류 3코드, 불확실한 쓰기·중복 방지·기본 안전장치를 검증한다. 공식 모의투자·시험 계좌·큐 고객 화면은 안 걸어 봄. 다음 단계에서 대표 승인과 시험 계좌로 검증해야 하며 이 문서는 실검증 표지가 아니다.
