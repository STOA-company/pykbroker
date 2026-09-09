---
description: "한국투자·DB증권·토스증권 Open API를 공통 Python SDK와 기본 읽기 전용 MCP 서버로 연결하는 pykbroker의 설치, 지원 범위와 안전장치."
lang: ko
---

# pykbroker

pykbroker는 한국 증권사(한국투자·DB증권·토스증권) Open API를 공통 파이썬 인터페이스와 기본 읽기 전용 MCP 서버로 묶은 Apache-2.0 라이브러리입니다.

pykbroker is an Apache-2.0 Python library that unifies Korean brokerage (KIS, DB Securities, and Toss Securities) Open APIs behind a common interface and a read-only-by-default MCP server.

| 구현 어댑터 | 오프라인 테스트 | Python |
| --- | --- | --- |
| **3곳** — KIS·DBFI·Toss | **141 통과** · 1건 환경 제약 skip | **3.11 이상** |

2026-09-09 실측입니다. 구현 수는 패키지의 `BROKERS` 등록 수이며, 테스트는 mock·합성 fixture 기반입니다. **실 API·모의계좌·실거래 검증은 수행하지 않았습니다.** skip은 sandbox의 로컬 IPC 제한입니다. [지원 표](BROKER_MATRIX.md)에 세부 범위가 있습니다. 초기 알파이며 PyPI 미게시 소스 기준 안내입니다.

## 30초 예제 — 한국투자증권 시세 조회

저장소 체크아웃에서 설치합니다.

```bash
pip install -e '.[mcp]'
```

증권사가 제공하는 인증 클라이언트와 환경변수를 [설치 안내](getting-started.md)에 따라 준비한 뒤:

```python
from pykbroker import create_broker

with create_broker("kis") as broker:
    quote = broker.get_quote("005930")
    print(quote.symbol, quote.last, quote.bid, quote.ask)
```

`PYKBROKER_ACCOUNT`에는 호출자의 계좌 식별자, `PYKBROKER_CLIENT_OPTIONS`에는 벤더 생성자 설정을 JSON으로 주입합니다. 시세 조회에도 인증이 필요할 수 있습니다. KIS·DB·Toss 벤더 SDK는 별도로 준비해야 하며, extras는 보조 의존성만 설치합니다. 30초 예제는 이 준비가 끝난 환경을 전제로 합니다.

키 없이 확인하려면 `python examples/offline_quote.py`를 실행하세요. 이 예제는 합성 응답을 사용하며 실제 시세가 아닙니다. 향후 배포판이 게시되면 `pip install pykbroker` 또는 `pip install 'pykbroker[mcp]'`를 사용할 수 있습니다.

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

## FAQ

### 한국투자증권 API로 파이썬에서 시세와 잔고를 어떻게 조회하나요?

호환 Pykis 소스와 인증 설정을 준비한 뒤 `create_broker("kis")`에서 `get_quote("005930")` 또는 `get_balance()`를 호출합니다. 이미 인증한 클라이언트와 API helper를 `KIS` 생성자에 주입할 수도 있습니다. [설치 안내](getting-started.md)와 실행 가능한 [한국투자증권 잔고 조회 예제](https://github.com/STOA-company/pykbroker/blob/main/examples/kis_balance_check.py)를 참고하세요.

### 한국 증권사 API를 클로드 데스크톱의 MCP 도구로 연결할 수 있나요?

`mcp` extra를 설치하면 `pykbroker-mcp` 명령으로 stdio MCP 서버를 실행할 수 있습니다. 기본 도구는 `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`이며, 조회를 위해 벤더 SDK와 인증 설정이 필요합니다. [Claude Desktop 설정 예제](claude-desktop.md)에 실행 경로와 JSON 설정이 있으며, 이 환경에서 Claude Desktop 연결 자체는 검증하지 않았습니다.

### 실계좌 주문 없이 모의계좌나 드라이런으로 테스트할 수 있나요?

기본 드라이런은 주문·취소 전송을 `DryRunError`로 차단하며, 모의 체결이나 성공 주문을 만들어 반환하지 않습니다. 키 없는 예제와 테스트는 mock·합성 fixture를 사용하므로 조회 데이터도 실제 시세가 아닙니다. 모의계좌 접속은 벤더 클라이언트 설정에 달려 있으며 공통 모의계좌 전환 기능과 실제 모의계좌 검증은 제공하지 않습니다.

### DB증권 Open API도 파이썬으로 사용할 수 있나요?

`create_broker("dbfi")` 또는 `DBFI`로 Pydb 클라이언트를 연결하는 어댑터가 구현돼 있습니다. KR·US 조회와 주문 변환, 단일 주문 취소를 오프라인 계약 테스트로 확인했으며 실 API 검증은 미실시입니다. US 잔고의 주문 가능 현금은 호출자가 `cash_provider`로 제공해야 하므로 [지원 표](BROKER_MATRIX.md)를 먼저 확인하세요.

### 토스증권 API는 어떻게 연동하고 무엇을 준비해야 하나요?

호환 `toss_openapi` 소스 SDK 또는 `sdk_factory` 브리지를 준비하고, `create_broker("toss")`에 필요한 `PYKBROKER_ACCOUNT`, `PYKBROKER_TOSS_ACCOUNT_SEQ`, `PYKBROKER_CLIENT_OPTIONS`를 환경으로 전달합니다. 비동기 SDK의 생성·호출·종료는 어댑터의 전용 이벤트 루프에서 처리하며 검증 범위는 mock 브리지입니다. `toss` extra만으로 벤더 소스가 설치되지는 않고, Toss SDK 자체의 드라이런·live 플래그도 별도로 적용됩니다.

### AI가 실주문을 보내지 못하도록 어떤 안전장치가 있나요?

SDK는 기본 드라이런이고, MCP는 유효한 `~/.pykbroker/limits.toml`에 `allow_orders = true`와 양수 `per_order_krw_cap`, `daily_krw_cap`가 있어야 주문·취소 도구를 등록합니다. `dry_run`을 생략하면 `true`이며 실제 전송에는 한도·사용량 저장소·멱등 저장소·킬스위치가 필요합니다. 설정 삭제·변경은 이후 MCP 주문을 차단하고, 차단기는 선택적으로 구성하며 자동 재시도는 하지 않습니다. 숫자 한도는 사용자가 정해야 하며 자세한 [MCP 설정](mcp.md)과 [안전 부품](safety.md)을 확인하세요.

### pykbroker는 무료인가요, 상업적으로 사용해도 되나요?

pykbroker 코드는 [Apache-2.0](https://github.com/STOA-company/pykbroker/blob/main/LICENSE)으로 제공되며 라이선스 조건을 지키면 상업적 이용·수정·배포가 가능합니다. 증권사 API 이용 조건·수수료와 별도로 준비하는 벤더 SDK 라이선스는 각각 확인해야 합니다. 출처와 재작성 범위는 [NOTICE](notice.md)에 있습니다.

### LS증권·키움증권·NH투자증권·KB증권도 지원하나요?

네 어댑터는 모두 **미검증 experimental**이며 3곳의 등록 어댑터 수에 포함하지 않습니다. LS·키움은 국내 요청 매핑이 있으나 주문 잠금 상태이고 조회 결과도 미검증이며, NH·KB는 네트워크 전송 없는 스켈레톤입니다. 최상위 팩터리에 등록되지 않았고, 잠금만 해제해 실사용할 수 있는 상태가 아닙니다.

## 관련 프로젝트

자매 프로젝트 [pycex](https://github.com/Jaeminyx-Stoa/pycex)의 패키징·fixture·FastMCP·문서 구조를 따릅니다.

문서와 기여:

- [설치·클라이언트 주입](getting-started.md), [영어 문서 / English documentation](en/index.md)
- [어댑터 지원 범위](BROKER_MATRIX.md), [새 어댑터 추가](BROKER_ADAPTER_RECIPE.md)
- [안전 원시 부품](safety.md), [모델](api/models.md), [CLI](cli.md)
- [실행 SDK 계약](execution-sdk-contract.md) — 문서와 타입만 제공
- [기여 안내](contributing.md), [보안](security.md), [변경 기록](changelog.md)

## 문서 주소와 AI용 텍스트

문서의 계획 주소는 `https://stoa-company.github.io/pykbroker/`입니다. 이 변경은 게시를 수행하지 않으며 주소의 공개 가용성은 검증되지 않았습니다. [llms.txt](llms.txt)는 요약과 주요 링크, [llms-full.txt](llms-full.txt)는 한국어·영어 문서 전체를 결합한 평문입니다. 파일 제공이 검색 수집이나 AI 인용을 보장하지는 않습니다.

[English](en/index.md)

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareSourceCode",
  "name": "pykbroker",
  "codeRepository": "https://github.com/STOA-company/pykbroker",
  "programmingLanguage": "Python",
  "license": "Apache-2.0",
  "author": {
    "@type": "Organization",
    "name": "Quantus Technologies"
  },
  "description": "한국투자·DB증권·토스증권 Open API를 공통 Python SDK와 기본 읽기 전용 MCP 서버로 연결하는 pykbroker의 설치, 지원 범위와 안전장치.",
  "inLanguage": "ko"
}
</script>
