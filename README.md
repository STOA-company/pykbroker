# pykbroker

pykbroker는 한국 증권사 Open API(한국투자·DB증권 등)를 하나의 인터페이스로 통합하는 파이썬 SDK와, 시세·잔고·주문 상태를 조회하는 MCP 서버를 제공합니다.

| 구현 어댑터 | 오프라인 테스트 | Python |
| --- | --- | --- |
| **3곳** — KIS·DBFI·Toss | **140 통과** · 1건 환경 제약 skip | **3.11 이상** |

구현 수는 패키지의 `BROKERS` 등록 수입니다. 실 API 검증 여부는 [지원 표](docs/BROKER_MATRIX.md)에 따로 표시합니다. 초기 알파 버전이며 PyPI 게시 여부는 **TBD**입니다.

## 시세 조회

```bash
pip install pykbroker
```

증권사가 제공하는 인증 클라이언트와 환경변수를 [설치 안내](docs/getting-started.md)에 따라 준비한 뒤:

```python
from pykbroker import create_broker

with create_broker("kis") as broker:
    quote = broker.get_quote("005930")
    print(quote.symbol, quote.last, quote.bid, quote.ask)
```

`PYKBROKER_ACCOUNT`에는 호출자의 계좌 식별자, `PYKBROKER_CLIENT_OPTIONS`에는 벤더 생성자 설정을 JSON으로 주입합니다. 국내 증권사 시세 조회도 인증이 필요할 수 있습니다. KIS·DB·Toss 벤더 SDK는 별도로 준비해야 하며, extras만으로 벤더 소스가 설치되지는 않습니다.

아직 게시 전인 체크아웃에서는 `pip install -e '.[mcp]'`로 설치합니다. 키 없이 확인하려면 `python examples/offline_quote.py`를 실행하세요. 이 예제는 합성 응답을 사용하며 실제 시세가 아닙니다.

## 동작 방식

```text
호출자 → StockBroker / BrokerAdapter 계약 → 증권사 어댑터 → 벤더 SDK
                      ↑                     │
                  MCP / CLI                 └ 주문: 안전 게이트 → 벤더 전송
```

조회는 공통 모델로 응답합니다. 주문·취소는 **드라이런 → 건당·일일 한도 → 멱등키 → 킬스위치 → 선택적 차단기 → 어댑터 전송**을 통과합니다. 한도·계좌·자격정보·상태 제공자는 호출자가 정합니다. 기본값은 주문 차단입니다.

## 역할

| 위치 | 하는 일 |
| --- | --- |
| `base` | 조회·주문·취소·종료를 정의한 공통 계약 |
| `brokers` | KIS·DBFI·Toss 변환 구현; 검증 범위는 오프라인 계약 테스트 |
| `experimental` | LS·키움의 미검증 매핑과 NH·KB 스켈레톤; 주문 잠금 유지 |
| `safety` | 호출자가 주입하는 한도·멱등성·드라이런·중단 판단 |
| `mcp` | 기본 읽기 전용 도구, 설정으로만 등록되는 주문 도구 |

## MCP와 주문 설정

```bash
pip install 'pykbroker[mcp]'
pykbroker-mcp
```

기본 도구: `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`.

`~/.pykbroker/limits.toml`에 `allow_orders = true`와 양수인 `per_order_krw_cap`, `daily_krw_cap`가 있어야 `place_order`, `cancel_order`가 등록됩니다. `dry_run`은 생략하면 `true`입니다. 숫자 한도는 제공하지 않으며 사용자가 정해야 합니다. 설정이 잘못되면 주문 도구가 노출되지 않습니다. [설정 방법과 차단 동작](docs/mcp.md)을 먼저 읽으세요.

## 문서와 기여

- [설치·클라이언트 주입](docs/getting-started.md)
- [어댑터 지원 범위](docs/BROKER_MATRIX.md), [새 어댑터 추가](docs/BROKER_ADAPTER_RECIPE.md)
- [안전 원시 부품](docs/safety.md), [모델](docs/api/models.md), [CLI](docs/cli.md)
- [실행 SDK 계약](docs/execution-sdk-contract.md) — 문서와 타입만 제공
- [기여 안내](CONTRIBUTING.md), [보안](SECURITY.md), [변경 기록](CHANGELOG.md)

Apache-2.0. 출처와 재작성 범위는 [NOTICE](NOTICE)에 있습니다. 자매 프로젝트 [pycex](https://github.com/Jaeminyx-Stoa/pycex)의 패키징·fixture·FastMCP·문서 구조를 따릅니다.
