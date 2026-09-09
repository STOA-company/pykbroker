# MCP Server

`pykbroker-mcp`는 `FastMCP`의 `@app.tool()`과 JSON 문자열 반환, stdio 전송 방식을 사용합니다. 기본 도구는 `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`입니다. `create_server()`와 모듈 전역 `mcp`가 제공됩니다.

## 주문 도구 등록

서버 시작 시 `~/.pykbroker/limits.toml`을 읽습니다. 다음 조건을 모두 충족해야 `place_order`, `cancel_order`가 추가됩니다.

1. 읽을 수 있는 유효한 TOML 파일.
2. `allow_orders`가 문자열이 아닌 boolean `true`.
3. `per_order_krw_cap`, `daily_krw_cap`가 유한한 양수.
4. `dry_run`은 boolean이며 생략 시 `true`.

빈 파일·잘못된 타입·누락 한도·비정상 금액은 주문 도구를 숨깁니다. 드라이런 도구 호출은 `DryRunError`로 거절하며 가짜 성공 주문을 반환하지 않습니다. 사용자가 실제 한도를 정하기 전에는 사용할 수 있는 한도 파일을 제공하지 않습니다.

다음은 **값을 입력해야 하는 템플릿**이며 그대로 실행 가능한 TOML이 아닙니다.

```text
allow_orders = true
per_order_krw_cap = <사용자가 정한 건당 원화 한도>
daily_krw_cap = <사용자가 정한 일일 원화 한도>
dry_run = true
```

등록 이후 파일을 삭제하거나 내용을 변경하면 주문·취소가 차단됩니다. 새 설정으로 도구 목록을 바꾸려면 서버를 재시작합니다. 설정 변경은 실행 중인 벤더 요청을 소급 취소하지 않습니다.

## 주문 호출과 저장

순서: 입력 검증 → 드라이런 → 한도 예약 → 멱등키 claim → 설정 재확인/킬스위치 → 선택적 차단기 → 벤더 호출.

- 기본 CLI MCP는 KR 주문을 사용합니다. US 자동 설정은 제공하지 않습니다.
- `place_order(symbol, side, quantity, client_key, price, order_type="limit")`.
- `cancel_order(order_id, symbol, quantity, client_key)`는 한 주문만 대상으로 합니다.
- 시장가 주문은 신뢰할 KRW 평가 상한과 정규장 판단을 주입해야 합니다. 기본 MCP는 이를 추측하지 않아 시장가를 차단합니다.
- KIS 단일 취소는 미지원이며 명시적으로 거절합니다. Toss는 한 주문의 전체 미체결분만 취소합니다.

`orders.sqlite3`는 한도 파일과 같은 디렉터리에 첫 주문 호출 시 생성됩니다. 계좌 식별자는 해시로 저장하고 client_key도 해시로 저장합니다. 한도 예약과 키 claim은 각각 원자적 트랜잭션입니다. 두 단계 사이 중단되면 한도만 소비될 수 있으나 제출은 진행되지 않습니다. 중복 요청·거절·타임아웃도 예약액을 돌려주지 않습니다. 따라서 한도는 체결액보다 보수적으로 소진됩니다.

일일 한도는 **계좌·브로커·KST 날짜별 제출 금액 합계**입니다. 매도는 매수와 합산합니다. 취소는 금액 0으로 한도 설정을 확인한 뒤 별도 취소 키 공간에서 중복을 차단합니다. KIS·DB는 네이티브 멱등키 지원을 주장하지 않습니다. 로컬 SQLite는 같은 파일을 사용하는 단일 호스트 프로세스들만 조정하며, 여러 호스트에는 공용 저장소를 주입해야 합니다.

## 앱에서 구성

```python
from pykbroker import DBFI
from pykbroker.mcp.server import create_server

app = create_server(
    broker_factory=lambda safety: DBFI(
        account=caller_account, client=caller_client, safety=safety
    )
)
app.run(transport="stdio")
```

주입 팩터리는 받은 `OrderSafety`를 그대로 사용하는 `BaseBroker`를 반환해야 합니다. 이를 바꾸는 팩터리는 거절합니다. 종료 시 생성된 어댑터를 닫습니다. 로그에는 브로커·마스킹한 계좌·동작만 남기며 원시 요청·응답·키를 기록하지 않습니다.

Toss SDK 자체의 드라이런과 live 플래그도 별도로 적용됩니다. 기본 팩터리는 이 추가 차단을 해제하지 않습니다. Toss 실주문 설정은 호출자가 명시적으로 구성한 `broker_factory`로만 주입하세요.
