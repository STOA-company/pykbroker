# Models

Pydantic v2의 불변 모델이며 알 수 없는 필드는 거절합니다. 금액은 `Decimal`로 유지하고 JSON에서는 문자열로 직렬화됩니다. 정수 수량에 boolean·문자열·소수 수량을 허용하지 않습니다.

| 모델 | 핵심 필드 |
| --- | --- |
| `Balance` | currency, cash_available, position_value; total 속성 |
| `Position` | symbol, market, quantity, sellable, average_price, last, market_value |
| `Quote` | symbol, market, last, bid, ask, prev_close, timestamp |
| `OrderRequest` | symbol, side, quantity, order_type, price, market, venue, client_key |
| `CancelRequest` | order_id, symbol, quantity, market, venue, client_key |
| `Order` | id, symbol, status, quantity, price, filled, remaining |
| `Rules` | symbol, market, currency, tick, lot, min_notional, regular_open, regular_close, timezone |
| `Capabilities` | 시장·단일 취소·시장가·NXT·주문 내역 지원 여부 |

`None`은 확인되지 않은 값입니다. `accepted`는 요청 접수이며 체결 또는 최종 취소를 의미하지 않습니다. 모델에 원시 응답이나 계좌·키를 저장하지 않습니다. `client_key`는 repr에서 숨기지만 호출자가 모델 전체를 직접 직렬화하면 포함되므로 로그로 출력하지 마세요.
