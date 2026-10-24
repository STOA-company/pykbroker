# 키움 국내 현금 주문 계약 모의 (2026-10-24)
- 기준 main: 2573cad, 가지 feat/kiwoom-contract-1024.
- 목표: 본인 키움 계좌 주문을 준비하는 공식 REST 계약 모의; 실주문 0.
- 경로: factory.create_broker("kiwoom") → brokers.Kiwoom → 기존 post 클라이언트 경계.
- experimental 경로는 정본이 아니며 KIWOOM_SPEC_VERIFIED=False 유지.
- 범위: 국내 KRX 현금 시장가·지정가 매수/매도, 정정·취소·조회·토큰·오류.
- 미국·신용·선물·큐 플랫폼 변경 없음.
## 공식 근거
- 포털: https://openapi.kiwoom.com/main/home 의 공식 GitHub/JSON 링크 확인.
- 공식 JSON SHA256: 42a7b3912c9d9588c83bdc2db7779c8d2e038703a2b5562e54ef46ae905cba79.
- 2026-10-24 공식 main 재다운로드가 기존 고정 커밋 69642586f7d84ba9fd8a6faf1f1537c7fda6568b와 일치.
- 계약 표·출처·미확인 칸: tests/fixtures/kiwoom/CONTRACT.md.
- kt10002 요청·응답 공식 예시와 관련 오류표 7행을 별도 fixture로 추출.
- 공식 /intro/serviceInfo §07은 접근토큰 24시간·재발급을 명시.
- 공식 /intro는 국내 계좌/토큰별 주문·조회 각각 5회/초, 모의 TR별 1회/초 명시.
## 바뀐 계약
- 시장가: trde_tp=3, ord_uv=""; 지정가: trde_tp=0, 원 정수 가격.
- KR market capability만 추가; US 지정가 계약 유지.
- 정정: Kiwoom.amend_order(AmendRequest), kt10002, 양수 수량·지정가 가격.
- AmendRequest는 OrderRequest를 확장하며 공통 broker protocol에는 정정 메서드 추가 없음.
- 기존 safety를 통과해 full amended notional 예약·dry-run·중복·중지 검사.
- idempotency action은 place/amend/cancel 타입으로 구분; 실패한 key도 보존.
- 정정 응답은 새 id·수량·가격·accepted; side/type 미확정 유지.
- 모주문 번호는 형식만 검사; 연쇄 정정의 즉전 id와 같다고 가정하지 않음.
- 정정 포함 원장의 기존 원주문 unknown 판정 유지; 완전체결 판정 유지.
- 토큰은 호출자가 명시 재발급·broker 재생성; 자동 재발급·주문 재전송 없음.
- kt10002=1회/초 추가로 국내 쓰기 합계 4회/초; 조회 배분 유지.
## 모의·오류·안전 검증
- 기존 가짜 post 클라이언트 꼴을 상태 서버로 확장; 실제 네트워크는 suite guard가 차단.
- 4종 주문, 부분체결→잔량정정→부분체결→취소→잔고/보유/원장 대사.
- 토큰 발급→만료 전송 차단→명시 재발급; 동일 safety의 key로 중복 방지.
- 1511·1902·8005·1700·1701·1702·1999를 주문/정정/취소에 모두 주입.
- 유량 초과 뒤 다음 합성 윈도 조회, 접수 후 응답 소실에서도 중복 없음.
- 기본 dry-run·중지·정정 위험금액·닫힌 broker·US 거절·잘못된 정정 모델 검사.
- Python 3.12 동일 런 venv 전체 base 1676 passed/19 skipped, head 1716 passed/19 skipped; 새 실패 0.
- 변경 production 4파일 bandit -q 무제외 통과. 테스트는 assert/가짜 자격값 B101/B105/B106 제외를 별도 기록.
- 변경 Python ruff check·git diff --check 통과; 한/영 지원표 키움 부분 동기화.
## 남은 것 (안 걸어 봄)
- 공식 모의투자·시험 계좌·큐 고객 화면·실체결·수수료·실제 정정 원장 전이 검증.
- expires_dt 시간대·재발급 영향·오류 HTTP 위치·윈도 경계·서버 idempotency는 미확인; 계약 표 참조.
