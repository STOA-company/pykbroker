---
description: "KIS·DBFI·Toss와 create_broker의 조회·주문 계약, 인스턴스 수명과 experimental 어댑터 제한."
lang: ko
---

# Brokers

`KIS`, `DBFI`, `Toss`와 `create_broker()`를 최상위에서 import합니다. 공개 조회는 `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`이며 변이는 `place_order`, `cancel_order`입니다. `BrokerAdapter` 어휘로 `quote`, `balance`, `positions`, `rules`, `place`, `cancel`, `status`, `close`도 제공합니다. 정산 인지 실행을 위해 `available_cash()`(주문가능현금)와 `available_qty(symbol)`(주문가능수량)을 제공하며, 두 호출은 매번 계좌를 다시 읽습니다 — 체결이 가용 잔고에 반영되기까지 지연이 있기 때문에 캐시하면 의미가 사라집니다.

클라이언트·계좌·safety는 인스턴스마다 명시적으로 결박됩니다. 계좌별 동시 호출 조정은 호출자가 책임지며 MCP는 어댑터 접근을 직렬화합니다. [지원 표](../BROKER_MATRIX.md)와 [설치](../getting-started.md)를 참고하세요.

## 모의투자 계좌

`KIS(..., virtual=True)`는 한국투자 모의투자 도메인으로 주문합니다. 계좌 모드는 추론하지 않습니다.

- `virtual`은 진짜 `bool`만 받습니다. `"true"`·`1` 같은 참값은 계좌를 승격·강등하지 못합니다.
- 주입한 클라이언트가 모드를 밝히지 않거나 요청과 다르면 호출을 거절합니다.
- `client_options`로 `virtual_account`를 밀어넣을 수 없습니다. 답은 한 곳에만 있습니다.
- 모의라고 안전 게이트가 느슨해지지 않습니다. 나간 주문에는 `Order.virtual`이 남습니다.
- 모의 도메인이 없는 브로커(DB증권·토스·experimental)는 `supports_virtual_account=False`로 선언하고 `virtual=True`를 거절합니다.

### 모의계좌에 실제로 있는 조회 경로

벤더는 모의계좌에 모든 조회를 열어주지 않습니다. 국내 미체결 조회(정정/취소 가능 주문)는 가상계좌에서 거부되고, 해외 미체결 조회는 실계좌 TR 하나만 있습니다. 그래서 조회 경로를 계좌 모드에 맞춰 나눕니다.

| 시장 | 모드 | `list_orders()` |
| --- | --- | --- |
| KR | 실계좌 | 미체결(정정/취소 가능) 조회 — 실시간 |
| KR | 모의 | 일별 주문 원장 — 벤더가 모의 TR 로 갈아주는 유일한 주문 경로. **지연될 수 있습니다** |
| US | 실계좌 | 해외 미체결 조회 |
| US | 모의 | **없습니다.** `supports_order_history=False`로 선언하고 `NotSupportedError`로 거절합니다 |

`capabilities().realtime_open_orders`가 그 조회가 실시간 스냅샷인지 지연된 원장인지 알려줍니다. 없는 경로를 있는 척하지 않고, 지연되는 경로를 실시간인 척하지도 않습니다.

### 거래소 명시

`capabilities().requires_venue`가 참인 시장(미국)은 주문·취소가 거래소를 **명시**해야 합니다. 브로커 인스턴스에 설정된 거래소로 조용히 대신 보내지 않습니다 — 아무도 이름 붙이지 않은 경로는 아무도 리뷰하지 않은 경로입니다. 거절은 안전 게이트 이전이라 위험 원장·멱등키를 소모하지 않습니다.

## capabilities()는 실제 동작과 일치합니다

실행기는 `capabilities()`를 보고 계획을 세웁니다. 미국 경로는 두 증권사 모두 **시장가가 없고** 거래소를 반드시 지정해야 하므로 `supports_market_order=False`, `requires_venue=True`, `venues=("NASDAQ","NYSE","AMEX")`로 선언합니다. 선언 목록 밖 거래소나 설정과 다른 거래소로 나가는 주문은 안전 게이트 이전에 거절됩니다.

`experimental` 어댑터는 최상위 `BROKERS` 팩터리에 등록하지 않습니다. 모듈을 직접 import해야 하며 기본 주문 잠금은 항상 닫혀 있습니다.

[English](../en/api/brokers.md)
