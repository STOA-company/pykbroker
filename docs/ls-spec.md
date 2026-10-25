# LS증권 Open API 국내 현금 주문 — 스펙 표

확인 시각: 2026-10-25 16:56 KST (07:56 UTC), 공개 문서 GET만 수행 — 로그인·키·계좌·실호출 0.
출처: TR 가이드 <https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true> (전체 sha256 `09274dbc6bd0c4ac14a9269091e335e846965bd43057a71b2ba4e38e32bbd242`, 버전 `20261022083305082` — 2026-10-24 고정본과 동일, 변경 없음). 고정 발췌: `tests/fixtures/ls/contract-20261024/`(CONTRACT.md 에 근거 표). 포털 <https://openapi.ls-sec.co.kr/apiservice>, 사용법 <https://openapi.ls-sec.co.kr/howto-use>.

## 엔드포인트 (POST, 헤더 `tr_cd`·`authorization: Bearer`)
| 기능 | 경로 / TR | 초당 한도(가이드) | 어댑터 |
|---|---|---|---|
| 토큰 | `/oauth2/token` (form: grant_type·appkey·appsecretkey·scope=oob) | 미기재 | `LS.issue_token`(명시 호출만) |
| 주문 | `/stock/order` / `CSPAT00601` | 10 | `_place_order` |
| 정정 | `/stock/order` / `CSPAT00701` | 3 | `amend_order(AmendRequest)` (옛 `modify_order(order_id, request)` 는 얇은 래퍼) |
| 취소 | `/stock/order` / `CSPAT00801` | 3 | `cancel_order` |
| 미체결·체결 | `/stock/accno` / `t0425` (`ordno·qty·cheqty·ordrem·price·cheprice`) | 2 | `list_orders`·`status` |
| 잔고·예수금·주문가능금액 | `CSPAQ12200` (`MnyOrdAbleAmt`·`BalEvalAmt`) | 1 | `get_balance`·`available_cash` |
| 보유·매도가능 | `t0424` (`janqty`·`mdposqt`) | 2 | `get_positions`·`available_qty` |
호스트: `https://openapi.ls-sec.co.kr:8080`. **모의 도메인: TR 가이드에 별도 호스트 기재 없음 — «모의투자는 A+종목코드»(IsuNo 설명)만 있어 모의투자 존재는 확인, 호스트·키 구분은 미확인.**

## 토큰·오류·한도
- 토큰: 가이드 `expire_in`(초, 문자열) / howto 예시 `expires_in=86400`. 어댑터는 둘 다 읽고 다르면 거절, 만료 시 **전송 전 차단**(주문 0건), 호출자가 명시 재발급.
- 오류·성공 코드: **공개 TR 가이드·FAQ 에 `rsp_cd` 의미 표가 없다**(0 으로만 이뤄진 코드는 공식 클라이언트 도우미 `isApiSuccessCode` 가 성공으로 해석). 그래서 `reject_codes.DEFINITIVE_REJECTS["ls"]` 는 **빈 표** — 쓰기의 0 이 아닌 모든 답은 unknown(재전송 0), 읽기는 거절. 출처 없는 코드는 넣지 않는다(SEAM §4).
- 호출 한도: TR 별 초당 건수만 문서에 있음(위 표) — 초과 시 코드는 문서 없음(계약 모의는 합성 `SYNTH_RATE`, unknown 처리). 계정 단위 총량 미확인.

## 주문 종류 표 (국내 현금, 2026-10-25)
`OrdprcPtnCode`(공식): 00 지정가 · 03 시장가 · 05 조건부지정가 · 06 최유리 · 07 최우선 · 12 중간가 · 61 장개시전시간외종가 · 81 시간외종가 · 82 시간외단일가. `OrdCndiTpCode`: 0 없음 · 1 IOC · 2 FOK. `MbrNo`: KRX / NXT(그 외 값은 KRX 로 처리됨).

| 종류 | API 지원(근거) | 어댑터 구현 | 모의 걸음(계약 모의) | amend_no_double_fill |
|---|---|---|---|---|
| 지정가 00 | 지원 | 구현 `limit` | 걸음 (`test_ls_contract_walk`) | 네이티브 정정 TR: 응답 분실(`TransportError`)·unknown 코드 → 재전송 0·새 주문 0 단언 |
| 시장가 03 | 지원 | 구현 `market` (OrdPrc=0) | 걸음 | 시장가로의 정정: API 는 받지만 통합 정정 모양은 «지정가 가격 변경»만 → `NotSupportedError`, 전송 0(취소→확정→새 주문은 호출자) |
| 조건부지정가 05 | 지원 | 구현 `conditional_limit` | 걸음 | 정정 대상은 지정가만 |
| 최유리 06 · 최우선 07 · 중간가 12 | 지원 | 구현 `best`·`priority`·`midpoint` | 걸음 | 해당 없음 |
| 장개시전시간외종가 61 · 시간외종가 81 | 지원 | 구현 `pre_close`·`after_close` | 걸음 | 해당 없음 |
| 시간외단일가 82 | 지원 | 구현 `after_single` (가격 필수) | 걸음 | 해당 없음 |
| IOC 1 · FOK 2 | 지원 | 구현 `time_in_force` | 걸음 | 해당 없음 |
| NXT | 지원 (`MbrNo=NXT`) | 구현 `venue="NXT"` (`supports_nxt`, venues `KRX·NXT`) | 걸음 (모든 종류 × KRX·NXT) | 정정 TR 에는 시장 칸이 없음(원주문 시장 유지) |
| SOR | 가이드에 값 없음(그 외 입력 = KRX 처리) | 미제공(조용히 KRX 로 가는 값을 열지 않는다) | 안 걸음 | — |
| 정정(가격) | 지원 (`CSPAT00701`) | 구현 `supports_amend=True`(KR) | 걸음: 부분체결→정정→취소 | 위와 같음 |
| 취소 | 지원 | 구현 | 걸음 | — |
| 신용(`MgntrnCode`≠000)·해외(COSAT00301) | 지원 | KR 현금 보통(000)만 · 해외는 기존 US 경로(지정가) | KR 신용 안 걸음 | — |

장전시간외종가·시간외종가·시간외단일가의 «실제 주문 가능 시간대»·종류별 가격 규칙은 문서에 없다 — 서버가 시간대·가격을 거절하면 그 답(코드 미확인)이 unknown 으로 남는다.

## 문서와 어댑터가 달랐던 곳 (고침)
- 시장가·지정가 2종·KRX·`OrdCndiTpCode=0` 고정 → 공식 코드 9종·NXT·IOC/FOK 를 데이터 칸(`LS_CODES`, `LS_KR_VENUES`)으로.
- 정정이 LS 전용 이름 `modify_order(order_id, request)` → 공통 `amend_order(AmendRequest)`, `Capabilities.supports_amend`.
- 시장가로의 정정은 이전엔 통과(OrdprcPtnCode=03)였으나 통합 모양(가격 변경)에서 거절로 바꿨다 — 안전 평가에 쓰는 가격이 사라지기 때문(지원 여부 변경이 아니라 모양 통일의 결과, 이유를 여기 남긴다).

## 미확인
성공/오류 코드표 · 모의 호스트 · 시간외 종류별 가격·시간대 규칙 · 토큰 실제 수명(07시 만료 여부) · 서버 멱등 키 없음 · t0425 상태 문자열 열거. 모의 왕복: 키 필요 — 이 머신에 없음(REPORT 참조).
