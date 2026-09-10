# Changelog

## 0.2.0 — Unreleased

큐 실행 프로그램 줄기 A′ — 주식 쪽 «거래소 문법»의 결손을 SDK 안에서 끝냅니다.

- **모의투자 계좌**: `KIS(..., virtual=True)`. 계좌 모드는 추론하지 않고, 밝히지 않거나
  어긋나는 클라이언트는 거절합니다. `Order.virtual`에 어느 모드로 나갔는지 남습니다.
- **규칙 확장**: `rules(symbol)`이 호가단위·세션·단주·거래정지·상하한가를 답합니다.
  `price_limits()`·`session_state()` 추가. 세션과 정지는 3상태(열림·닫힘·모름)이고
  `Rules.tradable`은 둘 다 확정일 때만 True입니다.
- **정산 인지**: `available_cash()`·`available_qty(symbol)`. 매 호출 계좌를 다시 읽습니다.
  `Position`은 «가용수량 > 보유수량» 행을 거부합니다.
- **정직한 capabilities**: `supports_limit_order`·`requires_venue`·`venues` 추가.
  미국은 시장가 미지원·거래소 필수를 계약으로 선언하고, 검증이 그 선언과 일치합니다.
- **계약 통일**: `virtual`·`halt_provider`가 모든 브로커의 같은 이름 인자입니다.
  experimental(LS·키움·NH·KB) 잠금은 그대로이며 팩토리에 등록되지 않습니다.
- `scripts/mutation_check.py` — 위 결정들을 되돌리면 스위트가 빨개지는지 확인합니다.

## 0.1.0 — Unreleased

- StockBroker/BrokerAdapter와 Pydantic v2 모델.
- KIS·DBFI 변환과 선택적 Toss 비동기 SDK 브리지.
- fail-closed 안전 부품, 주입형 정책, 로컬 SQLite 원장.
- 기본 읽기 전용 FastMCP 서버와 CLI.
- LS·Kiwoom 잠금 매핑, NH·KB 오프라인 스켈레톤.
- 실행 SDK 계약 문서와 실행 로직 없는 Protocol 타입.
- 오프라인 fixture/mock 테스트와 네트워크 차단.

벤더 소스의 설치 가능한 배포판 연결, LS·키움 US 확장, 실제 API 검증은 후속 과제입니다.
