# 실행 SDK 계약

이 문서와 `pykbroker.contracts.execution`은 실행기가 구현해야 할 **계약**을 정의합니다. 실행기, 코드 실행, 의도 디스패치, 재시작 복구, 스케줄러는 이 패키지에 없습니다. 이 계약의 어떤 함수도 실행 코드를 트레이더에 전달하지 않습니다.

## 1. 실행 경계와 고정 컨텍스트

실행 하나는 **계좌 1 · 종목 1 · 확정 수량 1 · 세션 1**에 묶입니다. 사용자가 확인한 뒤 계좌·종목·방향·목표 수량·선택 세션을 바꿀 수 없습니다. 변경은 새 실행으로 확인받아야 합니다. 계좌는 실행 시작 시 한 번 갱신한 읽기 전용 스냅샷이며 깊은 복사나 불변 저장으로 보장해야 합니다.

| 속성 | 계약 타입 | 의미 |
| --- | --- | --- |
| `ctx.account` | `Account` | `account_id`, `broker`, `cash_available`, `holdings` |
| `ctx.symbol` | `Symbol` | `code`, `name`, `market: KR/US/COIN`, `venue`, `tick`, `lot`, `min_notional` |
| `ctx.side` | `buy / sell` | 한 방향 |
| `ctx.target_qty()` | `int` | 확인한 총 수량(주) |
| `ctx.remaining()` | `int` | 목표 수량 − 해당 실행의 체결 누계 |
| `ctx.filled_qty()` | `int` | 해당 실행의 체결 누계 |
| `ctx.filled_avg()` | `float` | 해당 실행 체결의 가중 평균가 |

상태는 계좌 전체 체결이나 보유 수량 변화로 추정하면 안 됩니다. 실행 ID에 속한 체결 원장으로 누계와 잔량을 계산해야 합니다. 초기 미체결 상태의 평균가 표현은 실행기가 일관되게 정하고 문서화해야 합니다.

타입 표의 `ctx` 수량은 주식 정수 수량입니다. 의도 전송 타입의 COIN 수량은 float를 허용하지만, 이 패키지의 주식 브로커와 `ctx` 수량 계산을 소수 코인 수량으로 임의 확장하지 않습니다.

## 2. 수량 계산

순수 계산은 주문 의도를 만들지 않습니다. 계산 결과가 사용자 확인과 다른 주문으로 조용히 바뀌지 않도록 하기 위한 구분입니다.

| 함수 | 반환 | 실행기가 지킬 규칙 |
| --- | --- | --- |
| `qty_for_krw(krw: int, price: float)` | `int` 주 | `floor(krw / price)`; 0이면 `Clarify("금액이 1주 값보다 작습니다")` |
| `split(total: int, n: int, i: int)` | `int` 주 | 균등 분할, 나머지는 마지막 라운드; `1 ≤ n ≤ 40`; 인덱스는 이 계약에서 `0 ≤ i < n`으로 정의 |
| `round_price(price: float)` | `int` 원 | 종목 호가 단위로 매수 내림·매도 올림 |
| `held()` | `int` 주 | 매도 수량 상한으로 사용하는 보유 수량 |

0 이하 가격·잘못된 분할 개수·누락 종목 규칙은 확인 단계에서 `Clarify`로 처리해야 합니다. 호가·수량 계산 함수는 실행기 쪽에서 구현합니다. `rules.kr`은 별도 시장 규칙 도우미이며 이 파일에 실행기 구현을 추가하는 근거가 아닙니다.

## 3. 시세 읽기

트레이더 시세 프록시를 통해 조회하고 **지연 1초 이하, 실행당 초당 2회 상한**을 지켜야 합니다. 오래되거나 누락된 시세를 0으로 바꿔 주문 수량을 계산하지 않습니다.

| 함수 | 반환 |
| --- | --- |
| `last()` | 현재가 `float` |
| `best_bid()` / `best_ask()` | 매수·매도 1호가 `float` |
| `prev_close()` | 전일 종가 `float`, 가격 밴드 기준 |
| `vwap_today()` | 1분봉으로 계산한 당일 누적 VWAP `float` |
| `bars(n: int)` | 최근 n개 1분봉 `list[Bar]`, `n ≤ 390` |

`Bar`는 `t, o, h, l, c, v` 필드를 갖습니다. 시각과 가격은 종목의 거래일·통화와 일치해야 하며 실행기가 조회 제한·신선도 검증·계산을 책임집니다. 이 패키지의 `Quote`가 모든 프록시 지표를 제공한다고 가정하지 않습니다.

## 4. 시간과 대기

| 함수 | 반환 또는 규칙 |
| --- | --- |
| `now()` | KST `datetime`, timezone 포함 |
| `session()` | `Session{open, close, is_open}`; 확인 시 선택한 다음 거래일 세션 고정 |
| `wait(duration: str)` | `"30s"`~`"120m"`, 최소 30초; 장 마감에 도달하면 `ExecutionEnded` |
| `wait_until(hhmm: str)` | 선택 세션 안 시각만 허용; 밖이면 빌드 시 `Clarify` |
| `wait_for(pred, timeout: str)` | 30초마다 `pred()` 평가 |

실행기는 휴장·특별 개장일을 반영한 달력을 제공해야 합니다. 시간 비교는 선택 세션으로 수행하고 서버의 UTC 날짜나 단순 요일로 거래일을 추측하지 않습니다. 대기·폴링·재개 구현은 실행기 책임이며 이 패키지는 서명만 제공합니다.

## 5. 주문과 상태

**주문 함수 하나는 트레이더 의도 하나**에 대응합니다. 부분체결과 취소는 의도·실행 ID 기준으로 추적하며, 타임아웃을 새 주문으로 자동 변환하지 않습니다.

| 함수 | 반환 | 트레이더 의도 / 규칙 |
| --- | --- | --- |
| `place_limit(qty, price, ttl="5m")` | `Order` | `kind="limit"`, price, ttl_sec; TTL은 1~60분 또는 `until_close` |
| `place_market(qty)` | `Order` | `kind="market"`; 정규장에서만 |
| `cancel(order)` | 없음 | 해당 의도의 미체결분만 취소 |
| `cancel_open()` | 없음 | 이 실행의 열린 의도 전부 취소; 실행 종료 시 자동 호출 |
| `order.status()` | `Status` | 실행 조회 결과에서 해당 의도를 식별 |
| `order.wait(timeout)` | `Status` | 체결·취소·타임아웃까지 폴링 |

`Status`는 `state`, `filled_qty`, `avg_price`이며 state는 `open`, `partial`, `filled`, `cancelled`, `rejected` 중 하나입니다. 이 실행 계약의 `Order`는 SDK의 Pydantic `models.Order`와 별개인 행동 인터페이스입니다. 상태·대기를 구현하는 책임은 실행기에 있습니다.

TTL `until_close`는 고정 세션 종료까지 남은 시간으로 변환해야 합니다. 시장가 의도에 지정가를 끼워 넣거나, 단일 주문 취소를 계좌·종목 전체 취소로 확대하지 않습니다. 취소 응답을 체결이 사라졌다는 뜻으로 해석하지 않습니다.

## 6. 기록과 종료

| 함수 | 의미 |
| --- | --- |
| `note(text)` | 사용자에게 보이는 실행 로그 한 줄 |
| `done(reason)` | 조기 종료; 열린 의도 자동 취소 |

실행기는 기록에서 계좌·인증값을 제거하고 중복 종료를 안전하게 처리해야 합니다. 종료 요청 후 취소 결과를 원장으로 확인하는 책임도 실행기 소유입니다. 계약 패키지는 기록 저장·취소 루프를 실행하지 않습니다.

## 7. 예외와 안전 게이트

| 예외 | 발생 조건 | 실행기 처리 |
| --- | --- | --- |
| `Clarify(question)` | 빌드·드라이런 시 값 누락, 세션 밖 | 사용자에게 확인 요청 |
| `Rejected(reason)` | 한도·가격 밴드·킬스위치·장외 등 의도 거절 | 사용자 코드가 잡을 수 있음; 미처리면 실행 종료 |
| `ExecutionEnded` | 장 마감·런타임 상한·사용자 취소 | 열린 의도 자동 취소 후 종료 |

수량·소유권·세션·가격 밴드·드라이런·한도·멱등성·속도 제한을 트레이더가 확인해야 합니다. ctx나 SDK의 사전 검증으로 서버 측 권한·게이트 검증을 생략할 수 없습니다. 실제 한도와 승인 정책은 호출자가 제공합니다.

## 8. 트레이더 HTTP 계약

다음 경로는 **구현 요구사항**이며 pykbroker가 HTTP 서버로 등록하는 엔드포인트가 아닙니다.

```text
POST /orders/intents
{
  execution_id, seq, account_id, ticker,
  market: "KR" | "US" | "COIN", venue?, side: "buy" | "sell",
  qty, kind: "limit" | "market", price, ttl_sec, client_key
}
```

- KR·US `qty`는 정수, COIN은 실수. 가격·수량의 양수·유한성은 서버에서 검증합니다.
- `execution_id`는 실행 경계, `seq`는 실행 내 순서, `client_key`는 같은 의도의 재전송 식별입니다.
- 계좌 ID는 호출자가 전달한 계약상의 불투명 식별자이며 특정 저장소의 컬럼 규칙을 가정하지 않습니다.
- limit에는 price가 필요하고, market에는 지정가 제한을 암시하지 않습니다.

```text
200 { intent_id, status: "accepted", broker_order_id }
409 { status: "rejected", reason:
      kill_switch | dry_run | not_owner | cap_exceeded |
      price_band | session_closed | rate_limit | duplicate }

POST /orders/intents/{intent_id}/cancel
GET  /orders/executions/{execution_id}
     → { intents[], filled_qty, avg_price, remaining }
GET  /quotes/{ticker}
     → { last, bid1, ask1, prev_close, vwap }
```

`Trader` Protocol은 이 네 동작의 타입을 표현합니다. 의도 데이터는 숫자·문자열·상태만 전달하고 사용자 코드·함수·직렬화된 실행 코드를 보내지 않습니다. 인증, 소유권 확인, 디스패치, 체결 동기화, 재개·복구·게이트 정책 구현은 패키지 범위 밖입니다.

## 9. 실행기 구현자의 검증 기준

고정 컨텍스트 변경 거부, 분할 합계·마지막 잔량, 호가 반올림 방향, 거래일 경계, 오래된 시세, 조회 상한, 장외 시장가, 중복 의도, 부분체결 취소, 만료, 종료 정리, 권한 거절을 실행기 저장소에서 검증해야 합니다. 이 저장소는 Protocol의 모든 함수 본문이 `raise NotImplementedError`만 포함하는지 AST로 검사합니다.

함수 규칙이 문서에 있다고 실행기가 구현된 것은 아닙니다. 이 파일과 HTML 검토본은 같은 계약을 읽는 두 표면이며 Markdown이 정본입니다.
