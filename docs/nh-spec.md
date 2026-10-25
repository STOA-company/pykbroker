# NH PLUG (나무 / N2) 국내 현금 주문 — 스펙 표

확인 시각: 2026-10-25 16:56 KST (07:56 UTC), 공개 문서 GET만 수행 — 로그인·키·계좌·실호출 0.
출처(2026-10-24 고정본과 해시 동일, 그 사이 변경 없음):
- KR OpenAPI JSON <https://www.nhplug.com/openapi-docs/krstock/openapi.json> sha256 `678f27a5640f89508c37d73fc8c08a2804ff6fc0ebba9bfcb73b5fc5309bb9d2` (고정 추출본 `tests/fixtures/nh/official/krstock-1024.json`)
- 공통 OpenAPI <https://www.nhplug.com/openapi-docs/common/openapi.json>, LLM 가이드 <https://www.nhplug.com/llms-full.txt>, 오류표 <https://www.nhplug.com/testbed-howto>, SDK <https://github.com/PLUG-OpenAPI/nhplug-sdk>
- 이전 계약 모의 문서: `tests/fixtures/nh/official/CONTRACT-2026-10-24.md`

## 엔드포인트 (POST, 본문 `Input_0`, 응답 `Output_0`·`message`)
| 기능 | 경로 | 어댑터 |
|---|---|---|
| 토큰 | `/oauth2/token` (query: appkey·appsecretkey·grant_type·scope, 빈 body) · 24h | `NH.issue_token`, 만료 전 캐시 |
| 주문(매수·매도) | `/krstock/order/v1/cashBuy` · `cashSell` | `_place_order` |
| 정정 | `/krstock/order/v1/modify` (원시장주문번호·일부/전체·정정수량·정정가·정지조건가·시장) | `amend_order(AmendRequest)` |
| 취소 | `/krstock/order/v1/cancel` | `cancel_order` |
| 미체결·체결 | `/krstock/inquiry/v1/dailyOrderExecution` (통합주문번호 `itg_orr_no` 기준) | `list_orders`·`status` |
| 잔고·보유 | `/krstock/inquiry/v1/balance` | `get_balance`·`get_positions` |
| 주문가능 | 현금 = balance `orr_pbl_amt*`/`dca` 중 최소 · 매도가능 = `/krstock/inquiry/v1/sellableQuantity` | `available_cash`·`available_qty` |
호스트: 운영 `api.nhplug.com:8443`, **모의 `moapi.nhplug.com:8443`**(N2 브랜드 `api/moapi.n2plug.com`) — 토큰은 항상 운영 호스트에서 받고, 모의 계좌는 `acct_type=03` 만 유효.

## 토큰·오류·호출 한도
- 토큰 수명 24시간(발급 시각 기준). 조회는 `IGW40043/40044` 시 토큰 폐기 후 재발급·조회 1회 재시도, **주문은 폐기만 하고 재발급·재전송 0**.
- 오류표(공식): IGW40019 종목코드 · IGW40031/32 AppKey/Secret · IGW40043/44 무효/만료 토큰 · IGW42901/02/03 시스템/APP/APP+API 호출 초과 → `reject_codes.DEFINITIVE_REJECTS["nh"]` 데이터 행(출처 포함). **봉투 안 위치(`message.msg_code` vs `rsp_cd`)는 문서에 없다 — 어댑터는 둘 다 읽고 서로 다르면 거절.** 표 밖 코드·HTTP 429/500·시간 초과 = unknown(재전송 0).
- 호출 한도: REST 초당 약 5회(0.2초 간격 권장, LLM 가이드) — 로컬 4회/초. 정확한 수치·창은 미확인.

## 주문 종류 표 (국내 현금, 2026-10-25)
`nmn_pr_tp_cd`(호가유형) 공식 설명: 01 보통가 · 05 시장가 · 06 조건부지정가 · 09/10/11 자기주식류 · 12 최유리 · 13 최우선 · 16 스톱지정가 · 17 중간가 · 61 장전시간외 · 71 장후시간외 · 91/92 경쟁대량. `orr_cnd_dit_cd`: 00 없음 · 01 IOC · 02 FOK · 05 GTP. `rmt_mkt_cd`: SOR/KRX/NXT.

| 종류 | API 지원(근거) | 어댑터 구현 | 모의 걸음(계약 모의) | amend_no_double_fill |
|---|---|---|---|---|
| 지정가 01 | 지원 (cashBuy/Sell) | 구현 `limit` | 걸음 (`test_nh_contract_walk`) | 네이티브 정정 TR: 정정 응답 unknown·분실·HTTP 500 → 재전송 0, 새 주문 0 단언 |
| 시장가 05 | 지원, 가격·금액 생략(스키마 optional) | 구현 `market` (가격 생략 — 실서버 수용은 미확인) | 걸음 | 시장가 원주문 정정은 API 불가(가격 없음) → 전송 0 |
| 조건부지정가 06 | 지원 | 구현 `conditional_limit` | 걸음 | 정정 대상은 지정가만 |
| 최유리 12 · 최우선 13 · 중간가 17 | 지원 | 구현 `best`·`priority`·`midpoint` | 걸음 | 해당 없음(가격 없음) |
| 장전시간외 61 · 장후시간외 71 | 지원 | 구현 `pre_close`·`after_close` (가격 생략 — 실서버 수용 미확인) | 걸음 | 해당 없음 |
| 스톱지정가 16 | 지원(`sop_cnd_pr` 필요) | **미구현** — 트리거 주문 종류를 SDK 모델이 갖고 있지 않다(조건부 발동은 큐 의도 쪽 설계 필요) | 안 걸음 | — |
| 자기주식 09/10/11 · 경쟁대량 91/92 | 지원(기관·특수) | 미제공(개인 큐 범위 밖) | 안 걸음 | — |
| IOC 01 · FOK 02 | 지원 (`orr_cnd_dit_cd`) | 구현 `time_in_force="ioc"/"fok"` | 걸음 | 해당 없음 |
| GTP 05 | 지원 | 미구현(당일 아님 — 큐 의도 TTL 범위 밖) | 안 걸음 | — |
| NXT | 지원 (`rmt_mkt_cd=NXT`) | 구현: 인스턴스 `venue="NXT"` | 걸음 (KRX·NXT 모두) | 정정은 원장 행의 시장으로 전송 |
| SOR | 지원 (`rmt_mkt_cd=SOR`, `sor_mkt_sli_yn`) | 미구현(`UNT` 값은 기존 등록, 코드 의미 미확인) | 안 걸음 | — |
| 정정(가격) | 지원 (`modify`) | 구현 `amend_order(AmendRequest)`; `supports_amend=True`(KRX·NXT, KR) | 걸음: 부분체결→정정→취소 | 정정 응답 unknown → 재전송 0 (같은 키 재시도는 `DuplicateOrderError`), 새 주문 0 |
| 취소 | 지원 (`cancel`) | 구현 | 걸음 | — |

정정 입력(데이터 칸): `AmendRequest.broker_ref = {original_ledger_id: "itg:<번호>", stop_condition_price: "<Decimal 문자열>"}`. 이유: 정정은 시장주문번호(`mkt_orr_no`)로 하는데 원장은 통합번호(`itg_orr_no`)뿐이고 둘의 연결이 문서에 없다 — 호출자가 단언한다. `sop_cnd_pr` 는 스키마 required 이나 현금 지정가에서의 의미가 문서에 없어 기본값을 만들지 않는다.

## 문서와 어댑터가 달랐던 곳 (고침)
- 어댑터는 시장가·지정가만 보냈고 원장 이름 읽기도 3개 뿐 → 공식 호가유형 표로 종류 전체와 읽기 이름 표(`NH_KR_PRICE_TYPE_NAME`)를 데이터 행으로.
- IOC/FOK(`orr_cnd_dit_cd`)를 항상 `00` 으로 고정하던 것 → `time_in_force` 로.
- 공식 오류표 코드가 전부 `InvalidResponseError`(unknown)로 접히던 것 → 출처 있는 행은 `BrokerRejectedError(kind=rejected|token_expired|rate_limited)`.

## 미확인 (공식 모의 왕복에서 확인)
성공 코드 정본(`00000/00166/00221/13578` 은 기존 호환값) · 시장가·시간외의 가격 생략 수용 · 오류 코드가 오는 봉투 위치 · mkt↔itg 번호 연결 · 정정/취소 뒤 원장 최종 상태 · 실제 부분체결 시각 · 서버 멱등 키 없음(SDK 저장소가 막음). 모의 왕복: 키 필요(`NH` 앱키·시크릿·`acct_type=03` 모의 계좌) — 이 머신에 없음(REPORT 참조).
