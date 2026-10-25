# KB증권 Open API (B2C) 국내 현금 주문 — 스펙 표

확인 시각: 2026-10-25 16:56 KST (07:56 UTC), 공개 문서 GET만 수행 — 로그인·키·계좌·실호출 0.
출처: 개발 가이드 <https://openapi.kbsec.com/guide_b2c>, API 문서 <https://openapi.kbsec.com/apidoc_b2c>, XLSX <https://openapi.kbsec.com/api/kbs/guide/excel/b2c>, JSON ZIP <https://openapi.kbsec.com/api/kbs/guide/json/b2c>, 오류 목록 <https://openapi.kbsec.com/api/codes/public/B2C_error_code> (sha256 `ab7ccffcc70957f6baa4e9c6401548dd3a77ea430492faf3714a804df16fde01` — 10-24 고정본과 동일), 인증 예제 `kbsecurities/kb-openapi@5120174` `example/python/auth_example.py`.
XLSX 재다운로드 sha256 `1ff93a32…dae8` 는 10-24 판(`880b43b4…`)과 다르지만 **차이는 각 시트의 «최초작성일» 10-24→10-25 뿐**(4028행 비교, 그 외 동일) — 스펙 변경 없음. 계약 표 정본: `tests/fixtures/kb/official/CONTRACT-2026-10-24.md`.

## 엔드포인트 (POST, `appKey`·`Authorization: bearer`, 본문 `dataHeader`+`dataBody`)
| 기능 | 경로 | 어댑터 |
|---|---|---|
| 토큰 | `/oauth2/token` (`grantType=client_credentials`·appKey·appSecret) → `access_token·token_type·expires_in`(예시 86400) | `KB.issue_token`(명시 호출만) |
| 매수·매도 | `/api/v1/ssam1802` · `ssam1801` | `_place_order` |
| 정정 | `/api/v1/ssam1805` (`crct_clsf` 1 일부정정·2 전부정정, 원주문번호) | `amend_order(AmendRequest)` (일부정정 1 만 사용) |
| 취소 | `/api/v1/ssam1806` | `cancel_order` |
| 체결·미체결 | `/api/v1/ssqm2341` (`ccls_clsf` 0/1/2, `nxt_key` 연속) | `list_orders`·`status` |
| 예수금 | `/api/v1/ssqm0004` · 매수주문가능금액 `ssqm1802` | `get_balance`·`orderable_cash` |
| 보유·매도가능 | `/api/v1/ssqm2952` · `ssqm1801` | `get_positions`·`sellable_quantity` |
운영 호스트 `https://developer.kbsec.com:32484`. **모의 도메인: 문서(XLSX 전 시트 «운영환경» URL 뿐)·가이드에서 찾지 못함 — 없음으로 본다(미확인).** `issue_token(virtual=True)` 는 거절.

## 토큰·오류·한도
- 토큰: `expires_in`(예시 86400) 응답값 사용. 만료 코드 T043(공식 «접근 토큰 기간이 만료되었습니다»).
- 오류 코드(공식 목록 = 뜻만, **응답 내 위치·HTTP 상태는 문서에 없음**): 토큰 T041/T042/T043/T044 · 호출 초과 E041/I435/E042 · 허용 IP E031 · 권한 I446/T045 → `reject_codes.DEFINITIVE_REJECTS["kb"]` 데이터 행(출처 포함). 어댑터는 `dataHeader.resultCode`·`processCode` 중 «표의 키와 정확히 같은 값»만 확정 거절로 읽는다(표 밖 코드 T022·E002 등은 unknown). 토큰/한도 코드는 처리 전 거부 → 재전송 0.
- 호출 한도: 수치·창·Retry-After 없음(코드만). 로컬 1회/초는 보수적 제한이지 공식 한도가 아님.

## 주문 종류 표 (국내 현금, 2026-10-25)
`ordr_ccd`(SSAM1801/1802 입력 열거): 00 지정가 · 03 시장가 · 12 최유리지정가 · 13 최우선지정가 · M3 중간가. 출력 열거에만: 05 조건부지정가 · F0/F3/F5 FOK · I0/I3/I5 IOC · 99 시간외종가(SSAM1805 입력 열거에는 05 포함). `mkt_tm_clsf` 1 정규장 · 2 장개시전시간외종가 · 3 장종료후시간외종가. `sor_ordr_ccd` K KRX · N NXT · S SOR.

| 종류 | API 지원(근거) | 어댑터 구현 | 모의 걸음(계약 모의) | amend_no_double_fill |
|---|---|---|---|---|
| 지정가 00 | 지원 | 구현 `limit` | 걸음 (`test_kb_contract_walk`) | 네이티브 정정 TR: 응답 분실·unknown 코드 → 재전송 0·새 주문 0 단언 |
| 시장가 03 | 지원 (입력 열거) | 구현 `market` — `ordr_uprc` 필수 String(9) 이라 «0» 전송(시장가 값은 문서에 없음 → **미확인**) | 걸음(합성) | 정정은 지정가 가격 변경만 |
| 최유리 12 · 최우선 13 · 중간가 M3 | 지원 (입력 열거) | 구현 `best`·`priority`·`midpoint` (가격 «0») | 걸음(합성) | 해당 없음 |
| 조건부지정가 05 | 정정 입력 열거에만, 주문 입력 열거에는 없음 | **미구현** — 주문 입력으로 받는지 문서 모순 | 안 걸음 | — |
| IOC · FOK (I*/F*) | 출력 열거에만 | **미구현** — 입력 코드 조합 미확인 | 안 걸음 | — |
| 시간외종가(mkt_tm_clsf 2/3) | 입력 열거 있음, `ordr_ccd` 조합(99?) 미확인 | **미구현** — 추측 전송 금지 | 안 걸음 | — |
| NXT · SOR | 지원 (`sor_ordr_ccd`) | 구현: `venue="NXT"/"SOR"` (`supports_nxt`, venues `KRX·NXT·SOR`) | 걸음 (모든 종류 × 3) | 정정은 요청 venue 를 그대로 전송 |
| 정정(가격) | 지원 (`ssam1805`) | 구현 `supports_amend=True`(KR) | 걸음: 부분체결→정정→취소 | 위와 같음 |
| 취소 | 지원 (`ssam1806`, 수량 지정) | 구현 | 걸음 | — |
| 국내 소수점 주문 | 지원 (SSAM5762/5763/5764 · 조회 SSQM5765) | **미구현** — 이번 몫 밖(별도 스펙 읽기 필요) | 안 걸음 | — |
| 해외(미국) | 지원 (SKAM2101/2102 지정가류) | 기존 US 지정가·취소만, `supports_amend=False`(정정 TR 은 있으나 이번 몫은 KR) | 기존 시험 | 정정 미지원 → `NotSupportedError`, 전송 0 |

## 문서와 어댑터가 달랐던 곳 (고침)
- 시장가 코드(03)를 문서는 주면서 어댑터는 지정가만 → 입력 열거 5종을 데이터 행(`KB_KR_ORDER_CCD`)으로, NXT/SOR 은 `KB_KR_VENUE_CODE`.
- 공식 오류 목록이 어댑터에선 전부 `InvalidResponseError` 로 접히던 것 → 출처 행은 `BrokerRejectedError(kind)`.
- 정정이 `amend_order(request, *, order_id)` 였던 것 → 공통 `amend_order(AmendRequest)` (옛 모양은 호환 래퍼).

## 미확인
오류 코드의 응답 내 위치·HTTP 상태 · 수치 호출 한도 · 시장가 `ordr_uprc` 값 · 조건부/IOC/FOK/시간외 입력 조합 · 모의 도메인(없다고 본다) · 비어 있지 않은 국내 보유 수량 의미(SSQM2952) · 정정/취소 뒤 원장 관계. 모의 왕복: KB 는 모의 환경이 없어 실계좌 소액 시험(대표 승인)뿐 — 키 이 머신에 없음(REPORT 참조).
