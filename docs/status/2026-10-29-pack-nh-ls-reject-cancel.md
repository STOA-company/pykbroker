# NH·LS 확정 거절·LS 정정 체인 — 2026-10-29
- 범위: trader #676 검수 P1·P2. P3 지적은 0개였다.
- 고객 약속: 남아 있을 수 있는 주문을 거절·취소 완료로 확정하지 않는다.
- 구현 기준: pykbroker main a673c82cc9b064f01bbb065f526bc014b9b1d37e.
- 옵션 definite_rejects=False 기본과 trader의 OFF 상태는 바꾸지 않는다.
## 확정 거절 허용 목록
- 공식 NH 오류표: https://www.nhplug.com/testbed-howto (2026-10-29 수신).
- 코드별 아래 의미는 공식 표의 요약이다. 미접수 판단은 요청전 검증 의미에서의 추론이다.
- 공식 표 자체가 주문 접수 여부를 명시하는 별도 표는 아니다.
- IGW40012: JSON 형식 오류 — 요청을 해석할 수 없음. nh.py NH_DEFINITE_REJECT_CODES.
- IGW40018: 토큰에 계좌 없음 — 요청 계좌의 권한 없음. 같은 목록.
- IGW40050: Content-Type 오류 — 요청 형식 거절. 같은 목록.
- IGW40051: Bearer 유형 오류 — 요청 인증 형식 거절. 같은 목록.
- IGW40301: API 권한 없음 — 요청 권한 거절. 같은 목록.
- IGW40302: 법인 IP 비허용 — 접근 거절. 같은 목록.
- IGW40401: API URI 없음 — 실행할 API 없음. 같은 목록.
- 코드마다 소스 주석에도 같은 공식 URL을 남겼다. 양수/손상된 주문번호가 있으면 불명이다.
- 빈 문구·완료/접수/처리중/지연/timeout 문구도 허용 코드보다 우선하여 불명으로 남긴다.
- 90001·미등록·빈 코드는 제외. IGW40013·IGW50007·IGW50020 등 조회/응답 실패도 제외.
- 기존 NH HTTP401·IGW40043 토큰 거절은 그대로. IGW40043의 근거도 위 공식 표다.
- IGW40043을 새 business allowlist에 넣거나 토큰 갱신/주문 재전송을 추가하지 않았다.
- NH 공통 판정은 국내·미국 매수/매도/취소에 동일 적용한다.
- LS 허용 목록은 비어 있다. 03001의 확정 미접수 의미를 공식 자료에서 확인하지 못했다.
- LS 공식 TR: https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
- LS 공식 예제: https://openapi.ls-sec.co.kr/resource/playground/sample.html
- 전체 TR 20261026021444748와 예제에 주문 rsp_cd별 미접수 목록이 없었다.
- 일반 요청 실패 문구·OrdNo 부재만으로 미접수를 증명하지 않는다. 새 성공코드도 추가하지 않는다.
## LS 정정 체인
- t0425 전 페이지를 모은 뒤 orgordno 연결을 끝까지 탐색한다. 숫자 앞자리 0은 연결에서 정규화한다.
- 원주문→정정→재정정 중간 행은 cfmqty==qty여도 최종 취소 근거가 아니다.
- 후손이 있는 체인은 원주문을 unknown으로 보존한다. 열린 후손의 remaining과 원주문 filled도 보존한다.
- 후손 전량체결·후손 취소 역시 원주문 전체 합계를 추측하지 않고 unknown이다.
- 직접 취소의 기존 수량 계약은 유지. cfmqty가 취소 전용이라는 주장은 제거했다.
- 중복 주문번호·순환 연결은 InvalidResponseError로 거부하여 터미널 상태를 만들지 않는다.
## 검증·제한
- SDK 회귀: tests/test_nh_ls_reject_cancel_1029.py; 기존 1028 시험의 합성 코드 거절 기대를 정정했다.
- 정상 주문/부분체결/직접취소·모든 KR/US 쓰기·단발전송·같은 client_key 재전송0을 검사한다.
- trader 라우트 회귀는 원장과 실제 합성 HTTP 서버에 남은 주문을 함께 검사한다.
- 발주폴더의 공식 원본·수신시각·sha256·시험 XML/log가 근거다. 외부 DB/네트워크/Docker 소켓은 격리했다.
- dev/운영 고객 길은 안 걸어 봄. 계좌0/스위치OFF는 발주 전제이며 이번에는 운영을 조회하지 않았다.
