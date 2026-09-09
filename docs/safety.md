# Safety primitives

패키지에는 회사별 정책 숫자나 계좌 저장소가 없습니다. 각 부품의 기본 입력은 차단 방향입니다.

| 부품 | 주입 계약 | 비정상 입력 |
| --- | --- | --- |
| `DryRun` | 정확히 boolean `False`로만 live 허용 | 차단 |
| `KillSwitch` | `HaltProvider(broker, account) -> bool`, `False`만 허용 | 차단 |
| `RiskLimits` / `RiskLedger` | 건당·일일 KRW 한도와 원자적 예약 저장소 | 차단 |
| `IdempotencyStore` | `claim(scope, key) -> bool`, 최초 claim만 True | 차단 |
| `evaluate_breaker` | 손실률·오류율·횟수 관측과 임계값 | trip |
| `RateLimiter` | TR별 초당 횟수, 시계·sleep 함수 | 미등록 TR 차단 |

`OrderSafety`는 SDK와 MCP의 공통 제출 경계입니다. 기본 생성자는 드라이런 상태입니다. live 구성은 한도·사용량 저장소·멱등 저장소·킬스위치를 모두 요구합니다. 서킷브레이커와 rate limiter는 별도 구성 가능한 부품입니다. REST 실험 어댑터는 limiter 없이는 호출하지 않습니다. 기존 KIS/DB/Toss 클라이언트의 호출 제한은 벤더 SDK 또는 주입 클라이언트가 책임집니다.

한도는 `Decimal`로 계산하고 원화 지정가는 `수량 × 가격`을 기준으로 합니다. 별도 평가액을 주입하면 두 금액 중 큰 값을 적용합니다. US·시장가·시간외 주문에는 호출자가 보수적인 KRW 평가값을 제공해야 합니다. 시장가는 정규장 허용 콜백도 필요합니다. 실시간 가격 한 개만으로 시장가 체결액 상한을 보장할 수 없습니다.

`MemoryRiskLedger`, `MemoryIdempotencyStore`는 명시적으로 선택하는 단일 프로세스 테스트·데모용입니다. 재시작하면 기록이 사라집니다. MCP 기본 `SQLiteLedger`는 같은 파일을 쓰는 프로세스들에서 예약을 직렬화합니다. 어느 경우도 브로커에 이미 도착한 주문을 되돌리지 않습니다.

오류 후 자동 재시도는 하지 않습니다. 예약과 claim은 성공·실패에 관계없이 남깁니다. 불명확한 결과는 벤더 주문 원장에서 확인한 뒤 사용자가 새 의도 여부를 판단해야 합니다. 원장 파일을 삭제해 재시도하지 마세요.

`order_key()`는 계좌·브로커·종목·방향·수량·가격·주문 종류·회차를 반영합니다. 같은 숫자의 다른 표현은 동일한 키가 됩니다. 새 의도에는 새 회차 또는 client_key를 사용하며, 같은 의도의 재호출에는 기존 키를 보존합니다.
