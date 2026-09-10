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

### 2라운드 리뷰 반영 (A′2-1 ~ A′2-6)

- **모의계좌 주문 조회**: 국내 모의는 벤더가 모의 TR(VTTC8001R)로 갈아주는 일별 원장을
  읽습니다. 미국 모의는 벤더에 경로 자체가 없어 `supports_order_history=False` 로 선언하고
  `NotSupportedError` 로 거절합니다. 일별 원장은 실시간이 아니므로
  `Capabilities.realtime_open_orders` 로 그 사실을 밝힙니다.
- **거래정지**: `temp_stop_yn` 과 `iscd_stat_cls_code` 를 함께 읽습니다. 58·임시정지는 정지,
  벤더가 보낸 신호가 전부 «정상»일 때만 거래중, 그 외(51 관리·52 위험 등)는 모름입니다.
  `halt_provider` 통보는 거래소 행이 덮어쓰지 못합니다.
- **상하한가**: 벤더 정본 `stck_mxpr`/`stck_llam` 을 그대로 씁니다. ±30% 계산은 벤더가
  말하지 않았을 때만 쓰는 대체 경로이며 `Rules.limits_source` 로 구분됩니다.
- **거래소 강제**: `requires_venue` 인 시장은 주문·취소가 거래소를 명시해야 합니다.
  거절은 안전게이트 이전이라 위험 원장·멱등키를 소모하지 않습니다.
- **계약 호환**: `base.py` 변경은 전부 «추가»입니다. 기존 메서드는 그대로이고 `get_rules`/
  `rules` 의 `now`·`trading_day` 는 기본값 있는 키워드 전용이라, A′ 이전에 쓰인 줄기 B/B′
  의 호출부(`rules(symbol)`)가 그대로 바인딩됩니다. 새 메서드
  `available_cash()`·`available_qty(symbol)` 만 추가됩니다.
- **경계**: `experimental/_rest.py` 는 한 글자도 바뀌지 않습니다(잠금).
  `[dev]` extras 가 `mcp[cli]>=1.0,<2` 를 포함해 `pip install -e ".[dev]"` 로 전체 스위트가
  수집됩니다.

## 0.1.0 — Unreleased

- StockBroker/BrokerAdapter와 Pydantic v2 모델.
- KIS·DBFI 변환과 선택적 Toss 비동기 SDK 브리지.
- fail-closed 안전 부품, 주입형 정책, 로컬 SQLite 원장.
- 기본 읽기 전용 FastMCP 서버와 CLI.
- LS·Kiwoom 잠금 매핑, NH·KB 오프라인 스켈레톤.
- 실행 SDK 계약 문서와 실행 로직 없는 Protocol 타입.
- 오프라인 fixture/mock 테스트와 네트워크 차단.

벤더 소스의 설치 가능한 배포판 연결, LS·키움 US 확장, 실제 API 검증은 후속 과제입니다.
