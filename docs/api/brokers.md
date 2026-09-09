# Brokers

`KIS`, `DBFI`, `Toss`와 `create_broker()`를 최상위에서 import합니다. 공개 조회는 `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`이며 변이는 `place_order`, `cancel_order`입니다. `BrokerAdapter` 어휘로 `quote`, `balance`, `positions`, `rules`, `place`, `cancel`, `status`, `close`도 제공합니다.

클라이언트·계좌·safety는 인스턴스마다 명시적으로 결박됩니다. 계좌별 동시 호출 조정은 호출자가 책임지며 MCP는 어댑터 접근을 직렬화합니다. [지원 표](../BROKER_MATRIX.md)와 [설치](../getting-started.md)를 참고하세요.

`experimental` 어댑터는 최상위 `BROKERS` 팩터리에 등록하지 않습니다. 모듈을 직접 import해야 하며 기본 주문 잠금은 항상 닫혀 있습니다.
