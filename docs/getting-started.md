---
description: "pykbroker 소스 설치, KIS·DB·Toss 벤더 SDK 준비, 인증 환경변수와 클라이언트 주입 방법."
lang: ko
---

# Getting Started

Python 3.11 이상과 Pydantic v2를 사용합니다. 패키지 import는 인증·토큰 발급·소켓 연결을 수행하지 않습니다.

## 설치

```bash
# 현재: 게시 전 소스 체크아웃에서 설치
pip install -e '.[mcp,dev]'
# 향후 PyPI 배포판이 게시된 뒤에만:
# pip install pykbroker
# pip install 'pykbroker[mcp]'
```

| extra | 설치 범위 |
| --- | --- |
| `kis` | Pykis 실행에 쓰이는 보조 의존성 |
| `dbfi` | Pydb 실행에 쓰이는 보조 의존성 |
| `toss` | Toss 소스 SDK의 HTTP·설정 보조 의존성 |
| `mcp` | FastMCP 1.x |
| `dev` | pytest·pytest-asyncio·ruff·build |
| `docs` | MkDocs Material |

**벤더 코드 배포는 포함하지 않습니다.** 참조된 KIS 소스는 `Pykis._pykis`와 `Pykis.base`를, DB 소스는 `Pydb.pydbfi`를 사용합니다. 참조 버전의 설치 메타데이터는 이 대문자 경로와 일치하지 않아, 임의의 동명 PyPI 패키지를 설치하도록 연결하지 않았습니다. `kis`/`dbfi` extras는 보조 의존성만 설치합니다.

검토한 소스 트리를 별도 부모 디렉터리에 `Pykis/`, `Pydb/` 이름으로 놓고 그 부모를 Python import 경로에 추가하거나, 아래처럼 객체를 주입하세요. 참조 리비전은 [출처](notice.md)에 있습니다. 벤더 소스 라이선스와 접근 권한은 별도로 확인해야 합니다. `toss_openapi`도 참조 소스와 같은 모듈 구조의 SDK 또는 주입형 브리지가 필요합니다. 호환 배포판 연결은 TODO입니다.

## 환경변수

| 변수 | 뜻 |
| --- | --- |
| `PYKBROKER_BROKER` | `kis`, `dbfi`, `toss`, `ls`(기본 잠금·클라이언트 주입 필요); CLI/MCP 기본 `kis` |
| `PYKBROKER_ACCOUNT` | 호출자 소유 계좌 식별자; KIS에는 실제 벤더 계좌 형식 |
| `PYKBROKER_CLIENT_OPTIONS` | JSON 객체; 인증 생성자 인자를 환경에서 주입 |
| `PYKBROKER_TOSS_ACCOUNT_SEQ` | Toss가 발급한 accountSeq, 계좌번호와 구분 |

실값을 코드·명령 이력·문서에 작성하지 마세요. 외부 비밀 관리 도구가 환경변수를 주입하도록 구성합니다. CLI 인자로 계좌나 키를 받지 않습니다. 인증 옵션을 보관·갱신하는 정책은 SDK에 없습니다.

## 이미 인증된 클라이언트 주입

```python
from pykbroker import KIS, DBFI

# authenticated_client와 vendor_api는 호출자가 준비한 객체입니다.
# KIS: client.account(호출자 계좌), api.get_price_domestic(...), api.get_quote_domestic(...) 등.
broker = KIS(account=caller_account, client=authenticated_client, api=vendor_api)
quote = broker.get_quote("005930")
broker.close()

# DBFI는 Pydb DBFI 객체를 직접 사용하며 api helper를 요구하지 않습니다.
broker = DBFI(account=caller_account, client=authenticated_db_client)
```

주입한 클라이언트는 호출자가 소유하고 닫습니다. 어댑터가 `client_factory`로 만든 클라이언트는 지원되는 `close()`를 호출합니다. KIS는 `client.account(account)`로 계좌를 선택합니다. DB의 인증정보는 벤더에서 계좌에 결박되므로, 호출자가 전달한 식별자와 클라이언트가 같은 계좌를 가리켜야 합니다.

Toss에는 `sdk_factory`를 주입할 수 있습니다. 생성과 사용, 종료는 인스턴스 전용 이벤트 루프에서 수행됩니다. async 앱은 `await asyncio.to_thread(broker.get_quote, "005930")` 형태로 동기 API를 호출합니다. 호출 후 `close()`도 같은 방식으로 완료하세요.

## 거래 범위와 지원하지 않는 기능

KR·US 현물 정수 수량만 다룹니다. 파생상품·레버리지·포트폴리오 재조정·자동 재시도·실행기는 포함하지 않습니다. `get_rules()`의 `tick=None`은 가격대나 상품 구분이 없어 알 수 없다는 뜻입니다. 미지원 기능은 `NotSupportedError`를 발생시킵니다.

KIS/DB US 생성 시 `market="US"`, `venue="NASDAQ" | "NYSE" | "AMEX"`를 명시합니다. US 잔고에는 `cash_provider(client_or_scope) -> Decimal` 콜백으로 USD 주문 가능 현금을 제공해야 합니다. 특정 종목·계좌 유형을 임의로 선택하지 않습니다. US 주문의 KRW 한도 환산은 `OrderSafety.valuation`으로 주입합니다.

모든 벤더 클라이언트는 **주문 재시도를 끈 상태**로 공급해야 합니다. 이 패키지는 실패한 제출·취소를 재시도하지 않습니다. 벤더 SDK 안의 재시도 동작은 호출자가 확인해야 합니다.

[English](en/getting-started.md)
