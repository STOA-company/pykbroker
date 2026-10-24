# LS 국내 주식 현금 계약 모의 · 2026-10-24
상태: 오프라인 계약 시험용 공개 문서 고정본. 공식 모의투자·시험 계좌 검증을 뜻하지 않는다.
정본 어댑터는 `src/pykbroker/brokers/ls.py`; `experimental/ls.py`는 호환 alias다.
출처 A: https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
출처 B: https://openapi.ls-sec.co.kr/apiservice?api_id=54a99b02-dbba-4057-8756-9ac759c9a2ed&group_id=73142d9f-1983-48d2-8543-89b75535d34c · D: https://openapi.ls-sec.co.kr/howto-use
출처 C: https://openapi.ls-sec.co.kr/resource/playground/sample.html
확인 날짜: 2026-10-24 UTC. A 버전 `20261022083305082`; 공개 문서 GET만 수행했다.
A 전체 SHA256: `09274dbc6bd0c4ac14a9269091e335e846965bd43057a71b2ba4e38e32bbd242`.
각 JSON은 A의 transaction/path/defaults/commonHeaders를 그대로 발췌했다. 계좌·주문·토큰 응답값은 전부 합성이다.

| 기능 | POST 경로 / TR | 요청 핵심 필드 | 응답 핵심 필드 | 초당 제한 | 근거 |
|---|---|---|---|---|---|
| 토큰 발급·재발급 | `/oauth2/token` / token | form: grant_type=client_credentials, appkey, appsecretkey, scope=oob | access_token, token_type=Bearer, expire_in | 미확인(`-`) | A/B token.json |
| 시장가·지정가 매수/매도 | `/stock/order` / CSPAT00601 | IsuNo, OrdQty, OrdPrc, BnsTpCode=2/1, OrdprcPtnCode=03/00, MgntrnCode=000, LoanDt, OrdCndiTpCode=0, MbrNo=KRX | CSPAT00601OutBlock2.OrdNo(Number) | 10 | A CSPAT00601.json |
| 정정 | `/stock/order` / CSPAT00701 | OrgOrdNo(Number), IsuNo, OrdQty, OrdprcPtnCode, OrdCndiTpCode, OrdPrc | CSPAT00701OutBlock2.OrdNo(Number) | 3 | A CSPAT00701.json |
| 취소 | `/stock/order` / CSPAT00801 | OrgOrdNo(Number), IsuNo, OrdQty | CSPAT00801OutBlock2.OrdNo(Number) | 3 | A CSPAT00801.json |
| 계좌·예수금 | `/stock/accno` / CSPAQ12200 | BalCreTp=0 | OutBlock1.AcntNo, OutBlock2.MnyOrdAbleAmt/BalEvalAmt | 1 | A CSPAQ12200.json |
| 보유·매도가능 | `/stock/accno` / t0424 | prcgb=1, chegb=2, dangb=0, charge=0, cts_expcode | OutBlock1.expcode/janqty/mdposqt/pamt/price/appamt | 2 | A t0424.json |
| 주문·부분 체결·미체결 | `/stock/accno` / t0425 | expcode, chegb=0, medosu=0, sortgb=2, cts_ordno | OutBlock1.ordno/qty/cheqty/ordrem/price/cheprice | 2 | A t0425.json |

공통 JSON 요청 헤더: content-type, authorization, tr_cd, tr_cont, tr_cont_key; mac_address는 법인일 때 필수(A).
국내 종목번호: 실계좌는 6자리 또는 A+6자리, 모의투자는 A+6자리(A). 이번 fake는 native 실계좌 직렬화를 가로챈다.
연속 조회: 응답 tr_cont/tr_cont_key와 t0424/t0425 본문 CTS를 기존 어댑터가 함께 이어받는다(A).
토큰 수명: A/B 표는 `expire_in`(문자열, 초), D 응답 예시는 `expires_in`=86400이다. 둘 다 읽되 서로 다르면 거절한다.
86400은 예시이며 보장된 고정 수명·07시 만료는 미확인. 발급 시작 시각+반환 초로 expires_at을 계산한다.
만료 시 전송 전 차단→호출자가 명시 재발급→새 options로 재생성한다. app_secret 저장·자동 갱신·주문 재전송은 없다.
성공 코드: C의 실제 token-present 요청 도우미 `isApiSuccessCode`가 all-zero를 성공으로 해석한다. 주문별 공식 성공표는 미확인.
오류 코드: 공식 REST rsp_cd 의미 표를 A/B/C·공개 FAQ에서 찾지 못했다. 대표 오류 3개의 공식 매핑은 **미확인**이다.
`SYNTH_AUTH`, `SYNTH_RATE`, `SYNTH_ORDER`는 시험에서만 쓰는 가짜 코드이며 LS 오류 코드가 아니다.
시험은 위 3개 미지 코드의 읽기 거절·쓰기 결과 모름, 합성 HTTP 401/403/429, 제한 초과를 확인한다. 공식 코드 검증으로 세지 않는다.
부분 체결: qty=주문수량, cheqty=체결수량, ordrem=미체결잔량(A). cheprice는 체결가격이고 평균 정의는 없어 fill_price=None 유지.
취소·정정 접수는 accepted만 반환한다. 잔량0만으로 취소/체결을 추측하지 않는다. t0425 상태 문자열 열거값은 미확인이다.
정정은 LS 전용 `modify_order(order_id, OrderRequest)`이며 기존 OrderSafety place namespace를 재사용한다. 매수/매도 방향 변경 필드는 없으므로 입력 side를 전송·반환하지 않고 응답 side=None을 유지한다.
중복 방지는 SDK의 기존 client_key 저장소 범위다. 브로커 idempotency/client-key 필드는 A에 없고 서버 차원 보장은 미확인이다.
동일 key는 place/modify 사이에도 다시 쓰지 못하며, 응답 손실·미지 오류 후 claim을 보존하고 조회로 대사한다.
가짜 서버는 기존 FakeClient 시험 꼴을 HTTP 경계로 옮겼다. 공식 필드와 자료형 검사는 production LS_CODES를 참조하지 않는다.
소켓 차단은 기존 tests/conftest.py가 유지한다. LS_SPEC_VERIFIED=False·기본 live opt-in OFF·큐 venue 표는 그대로다.
계약 시험 표지는 `tests/test_ls_official_contract.py` 통과 목록이다. 실검증 플래그를 올리지 않는다.
남은 검증: 공식 오류/성공 코드 확정, 계좌 모드·토큰 실제 만료 계약, 공식 모의/시험 계좌 주문·정정·취소·체결 대사.
이 문서는 pykbroker LS 영역 팩을 겸한다. 실주문·실 API·키·운영 접근·머지·배포는 이번 계약 모의에 포함하지 않는다.
