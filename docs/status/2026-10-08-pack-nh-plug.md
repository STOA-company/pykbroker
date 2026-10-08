# NH PLUG 명세 대조 — 2026-10-08
## 범위·전제
- 시작 a7bf2a84에는 #23을 통한 미국 지정가·취소·원장과 KR Output_0/Output_1 수용이 이미 있다.
- #20(0730bd5e) 대비 남은 차이는 US 규칙·이전 종가이며 기존 안전 수정은 되돌리지 않았다.
- 증권사 호출 0건; 운영 VM·DB·키 발급·실/모의 거래·배포·독립 리뷰·머지 없음.
## 고친 것
- 국내 현금은 있는 dca·orr_pbl_amt4(100%)·구 응답 orr_pbl_amt의 최솟값.
- 30%·40% 기준 orr_pbl_amt2/3는 현금으로 쓰지 않는다; 후보 없음/잘못된 값은 실패.
- 잔고 필드·보수적 현금 매핑은 **모의 응답으로 확정 전**.
- dailyOrderExecution은 공식 Output_0 배열 우선; 빈 배열도 우선, 구 Output_1 호환은 보존.
- client_options.brand=nhplug(나무 기본)/n2plug(N2); factory 옵션·환경 JSON 전달을 시험.
- 허용 주소는 HTTPS :8443의 api/moapi.nhplug.com 및 api/moapi.n2plug.com 네 곳.
- 토큰·계좌 확인·시세는 같은 브랜드 api, 모의 계좌·주문·취소는 같은 브랜드 moapi.
- 두 운영 호스트 모두 allow_live_orders=False 기본 잠금; 기존 안전 게이트·토큰 정책 보존.
- 미국 buy/sell/cancel/unexecuted 스키마를 고정하고 주문·원장의 같은 orr_no를 시험.
- 미국 매도에는 명세에 없는 wtm_cur_knd_cd를 보내지 않는다; 매수는 해당통화 1 유지.
- KR mkt:↔itg: 연결은 모름; US us: 번호·뉴욕 오늘·평균 체결가 미확인 정책 유지.
## 공식 근거
- 인증·계좌: https://www.nhplug.com/openapi-docs/common/openapi.json
- 잔고·국내 내역: https://www.nhplug.com/openapi-docs/krstock/openapi.json
- 미국: https://www.nhplug.com/openapi-docs/gbstock/openapi.json
- 보안 알림: https://www.nhplug.com/llms.txt
- 브랜드·도메인: https://github.com/PLUG-OpenAPI/nhplug-sdk (README·nhplug/auth.py)
- tests/fixtures/nh/official/SOURCE.md에 수신 KST·원본 sha256; 공개 스키마만 발췌.
## 모의로만 확인할 것 (이번에는 호출하지 않음)
- 잔고 dca·orr_pbl_amt4·구 필드 실제 유무와 주문 가능 현금의 의미.
- 헤더 x-client-id: 기존 구현은 유지, 공개 JSON의 명시 근거·실제 요구 여부 미확인.
- 재발급 보안 알림: 공식 안내에는 유발한다고 명시; 수신자·채널·횟수는 미확인.
- 국내 시장주문번호 mkt_orr_no↔통합주문번호 itg_orr_no의 실제 대응은 미확인.
- 미국 모의 제공 범위·야간 orr_dt의 날짜 관례·cns_pr 평균 여부는 미확인.
## 검수·되돌리기
- 오프라인 시험만 제출; main 머지는 다른 계열 독립 리뷰·재검수·대표 승인 뒤.
- 회귀 명령: PYTHONPATH=src /home/quantus/coding/pykbroker/.venv/bin/python -m pytest -q tests -p no:cacheprovider
- 배포물은 없음. 되돌리기는 이 작업의 수정 커밋 revert 후 같은 오프라인 회귀.
