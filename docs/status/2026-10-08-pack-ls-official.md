# LS 공식 TR 정합화 — 2026-10-08 (bx-w3-ls)
상태: 오프라인 변경 체크포인트, 고객 출하 아직 아니다; 머지·배포 없음.
브랜치 codex/ls-official-1008 → base codex/sdk-integration-1004, draft·do-not-merge.
기준 HEAD a7bf2a84562073807118ab06b2bd83c122a1bb2c (#27).
#21 d7b6e186·#23 3cce252b는 기준 HEAD 조상; 미국 매핑·날짜 잠금 이미 존재.
전제 MAP.md pykbroker 행은 로컬 mono MAP에 없음; 발주서·기존 LS 팩으로 범위 고정.
공식 원본 https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
버전 20261007070834325; 수신 2026-10-08 09:08:06.978070 KST.
원본 SHA256 07e14d740de55502caf0ecfe6860047c8690018e628b3231cf7a7ba86324c5fa.
고정본 tests/fixtures/ls/official/: 지정 9개 TR·token, 기본값·머리글 상속 보존.
SOURCE.md에 출처·시각·버전·원본/발췌 SHA256; 발췌 10개를 원본과 직접 대조.
주문 응답은 기존 성공 해석 코드 + 양수 OrdNo를 공통 경계에서 모두 요구.
CSPAT00601·CSPAT00801·COSAT00301 주문/취소의 번호 없음·0·잘못된 타입은 모름.
공식 TR 가이드에 주문 rsp_cd 성공표/응답 예시 없음; 00039/00040 성공은 미확인.
비영 코드 + 양수 OrdNo도 InvalidResponseError(접수 여부 확인 필요), 실패 확정 아님.
기존 all-zero 판정 근거는 10-04 팩의 공식 token-present 클라이언트; 새 성공값 추가 없음.
exceptions.py:24-29는 쓰기 InvalidResponseError를 모름·대사 필요로 정의.
safety/gate.py:78-89는 실패 시 예약 금액·client_key를 보존; SDK 재전송 루프 없음.
외부 실행기/큐의 재시도 정책은 미확인; contracts/execution.py는 타입 계약만 제공.
IsuNo 공식 설명: 실 주식/ETF는 종목코드 또는 A+종목코드, 모의는 A+종목코드.
실 요청 6자리 유지, 합성 모의 주문·취소 둘 다 A+6자리로 수정; 응답 종목은 정규화.
t0424·t0425·COSAQ00102는 응답 tr_cont/tr_cont_key를 다음 요청에 전달.
국내 두 TR의 CTS 본문 필드도 다음 요청에 복사; 기존 LS_RATES로 매쪽 제한.
20쪽 상한·반복 키·머리글/CTS 불일치·뒷쪽 손상은 부분 목록 반환 없이 오류.
미국 COSOQ00201 잔고 이어받기는 범위 밖, 기존 최종쪽 요구 유지.
COSAT00301 요청에는 날짜 없음; COSAQ00102 OrdDt는 8자리 주문일자만 정의.
날짜의 거래일·시간대 설명 없음; 주문 전 명시 broker order_date 잠금 유지.
#21 비교의 차이는 기존 날짜/시세/규칙 보호를 제거하는 방향뿐; 가져올 누락 동작 없음.
미국 AAPL 지정가 필드·단축종목·매수02·NASDAQ82·지정가00를 공식 필드 시험으로 고정.
allow_live_orders 기본 False, 계좌/안전문·모의주소 None·평균체결가 None 유지.
정정·예약·실시간·시장가 확장·토큰 발급/갱신·잠금 해제는 안 함.
증권사 서버 호출 0건, 실제/모의 고객 길 안 걸어 봄; VM·DB·Azure·큐 접속 없음.
기준선: 지정 전체 pytest 1318 passed, 5 failed, 19 skipped (40.13s).
끝: 같은 전체 pytest 1398 passed, 5 failed, 19 skipped (44.40s); 추가 실패 없음.
기존 실패 5개: KB 등록표·NH US·DBFI 현금·LS 잠금 문구 regex·KIS US 규칙.
LS 전용 중간 시험 304 passed; 이후 추가 5개도 끝 전체 시험에서 통과.
ruff 변경 소스/시험 통과; 같은 기존 실패 재관측 후 추가 수정·시험 중단 (실행 총 3회).
모의/LS 서면 답으로 확정: 주문 성공 코드 최종값·모의 REST 주소·토큰 07시 만료·IP 제한.
토큰 가이드는 expire_in 초만 정의; 날짜 추정·토큰 담당은 만들지 않음.
main 머지는 다른 계열 독립 리뷰·재검수 + 대표 승인 뒤; 자체 리뷰 호출 0건.
