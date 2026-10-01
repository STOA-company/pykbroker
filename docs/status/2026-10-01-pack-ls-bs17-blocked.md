# BS-17 LS 정식 어댑터 — 공식 상세 명세 조회 막힘
상태: 막힘. 구현·승격·잠금 해제는 시작하지 않았다.
확인일: 2026-10-01 UTC, bs17-ls-1001 작업자.
범위: 오프라인 LS KR 어댑터, 체결가, 합성 계약 시험.
제외: dev·운영·DB·VM·배포·브라우저·거래 API 호출.
기준 main: c9dbecde08e22a1e3ec6a041d86dff11e1be36de.
기준 trader master: 8e3b7d43e6ddefadaf9a4206e151f7092c776bed.
기준 ref는 git ls-remote origin main/master로 확인했다.
작업 브랜치: feat/ls-adapter-1001.
기존 구현: src/pykbroker/experimental/ls.py.
기존 LS_SPEC_VERIFIED=False는 그대로다.
factory.py와 BROKERS 등록표도 그대로다.
레포 AGENTS.md·CLAUDE.md·MAP.md는 작업 트리에 없었다.
승격 절차: docs/BROKER_ADAPTER_RECIPE.md §1~6.
절차는 공식 계약 증거·안전 게이트·합성/녹화 구분을 요구한다.
계획 근거: plan-broker-support.md §4.6·§9.2 BS-17.
이음매 근거: map-m6-brokers.md §3의 LS 항목.
규칙 엔진 근거: spec-q-rule-engine.md §9.7 체결 확인 조건.
재사용 대상: BaseBroker, OrderSafety, decimal, whole, RateLimiter.
참고 주문 코드: trader quantus_backend/traders/ls_order.py.
참고 코드의 매수/매도·주문 방식 등 열거값도 None으로 잠겨 있다.
그 값을 사실로 확정하거나 추정값으로 채울 근거가 없었다.
Order 모델에는 체결가 칸이 없고 지정가 price만 있다.
체결가 칸 추가·t0425 매핑·부분 체결 시험은 아직 미구현이다.
공식 목록: https://openapi.ls-sec.co.kr/apiservice
공식 주문: https://openapi.ls-sec.co.kr/apiservice?api_id=d0e216e0-10d9-479f-8a4d-e175b8bae307
공식 공개 메뉴: https://openapi.ls-sec.co.kr/guide/menu?type=OPENAPI
공식 사이트의 apiguide JavaScript에서 상세 문서 경로를 확인했다.
첫 t1102 상세 요청은 HTTP 500으로 실패했다.
첫 요청의 type=OPENAPI를 공식 JavaScript 기본값 rest로 바로잡았다.
두 번째 t1102 상세 요청도 HTTPError로 실패했다.
두 번째 상태번호는 출력하지 않아 확정하지 않는다.
두 실패 뒤 발주서의 중단 규칙을 적용했고 추가 조회를 멈췄다.
접근한 것은 공개 문서 HTTPS 443뿐이며 거래 API 8080 호출은 없다.
전체 회귀: PYTHONPATH=src /home/quantus/coding/pykbroker/.venv/bin/python -m pytest -q
결과: 779 passed, 19 skipped in 22.74s, 종료까지 기다렸다.
이 결과는 기존 코드 회귀이며 BS-17 새 계약 통과 증거가 아니다.
수정 전 실패→통과 시험: 없음. 새 코드·fixture를 만들지 않았다.
재개 조건: 공식 상세 명세 또는 공식 다운로드 원본을 확보해 별도 재발주.
출하 판정: 아직 아니다. 본 PR은 막힘 기록이며 독립 검수를 대체하지 않는다.
