---
description: "pykbroker unifies KIS, DB Securities and Toss APIs in a Python SDK and read-only-by-default MCP server. Installation, support limits and safety."
lang: en
---

# pykbroker

pykbroker is an Apache-2.0 Python library that unifies Korean brokerage (KIS, DB Securities, and Toss Securities) Open APIs behind a common interface and a read-only-by-default MCP server.

pykbroker는 한국 증권사(한국투자·DB증권·토스증권) Open API를 공통 파이썬 인터페이스와 기본 읽기 전용 MCP 서버로 묶은 Apache-2.0 라이브러리입니다.

| Implemented adapters | Offline tests | Python |
| --- | --- | --- |
| **3** — KIS, DBFI, Toss | **141 passed**, 1 environment-related skip | **3.11+** |

Measured on 2026-09-09. The adapter count is the number of entries in `BROKERS`; tests use mocks and synthetic fixtures. **Real APIs, paper accounts, and live trading have not been validated.** The skip concerns sandbox restrictions on local IPC. See the [support matrix](BROKER_MATRIX.md). This is an alpha, unpublished source checkout.

## 30-second example: KIS stock quote

Install from the checkout:

```bash
pip install -e '.[mcp]'
```

Prepare the vendor authentication client and environment as described in [getting started](getting-started.md), then run:

```python
from pykbroker import create_broker

with create_broker("kis") as broker:
    quote = broker.get_quote("005930")
    print(quote.symbol, quote.last, quote.bid, quote.ask)
```

Set `PYKBROKER_ACCOUNT` to the caller's account identifier and inject vendor constructor settings as JSON through `PYKBROKER_CLIENT_OPTIONS`. Quote requests may require authentication. Obtain KIS/DB/Toss vendor SDK sources separately; extras install supporting dependencies only. The 30-second example assumes that preparation is complete.

For a key-free demonstration, run `python examples/offline_quote.py`. It returns synthetic responses, not live market data. After a distribution is published, installation can use `pip install pykbroker` or `pip install 'pykbroker[mcp]'`.

## How it works

```text
Caller → StockBroker / BrokerAdapter contract → Broker adapter → Vendor SDK
                         ↑                          │
                     MCP / CLI                      └ Orders: safety gate → vendor submission
```

Queries return shared models. Orders and cancellations pass through **dry-run → per-order/daily caps → idempotency key → kill switch → optional circuit breaker → adapter submission**. The caller supplies limits, account, credentials, and state providers. Orders are blocked by default.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `base` | Shared query, order, cancellation, and shutdown contracts |
| `brokers` | KIS/DBFI/Toss conversions and default-locked LS; offline contract tests |
| `experimental` | LS compatibility alias and other unverified mappings/skeletons; writes stay locked |
| `safety` | Caller-injected limits, idempotency, dry-run, and halt decisions |
| `mcp` | Default read-only tools; order tools registered only through configuration |

## FAQ

### How do I query Korea Investment Securities quotes and balances in Python?

Prepare compatible Pykis source and authentication settings, then call `get_quote("005930")` or `get_balance()` on `create_broker("kis")`. Alternatively, inject an authenticated client and API helper into `KIS`. See [getting started](getting-started.md) and the complete [KIS balance example](https://github.com/STOA-company/pykbroker/blob/main/examples/kis_balance_check.py).

### Can I connect Korean brokerage APIs to Claude Desktop through MCP?

Install the `mcp` extra and run `pykbroker-mcp` as a stdio server. Its default tools are `get_quote`, `get_balance`, `get_positions`, `list_orders`, and `get_rules`; queries require vendor SDKs and authentication. The [Claude Desktop guide](claude-desktop.md) provides paths and JSON configuration, but actual Claude Desktop connectivity has not been validated in this environment.

### Can I test without live orders using a paper account or dry-run?

Default dry-run blocks submission and cancellation with `DryRunError`; it does not simulate fills or return successful orders. Key-free examples and tests use mocks and synthetic fixtures, including synthetic quote data. Paper-account connectivity depends on vendor client configuration; the package provides neither a shared paper-account switch nor validated paper-account integration.

### Does the Python library support DB Securities Open API?

`create_broker("dbfi")` and `DBFI` provide an implemented adapter for a Pydb client. KR/US queries, order conversions, and single-order cancellation have offline contract checks, with no real API validation. US orderable cash requires an injected `cash_provider`; check the [support matrix](BROKER_MATRIX.md).

### How do I integrate Toss Securities and what do I need?

Obtain compatible `toss_openapi` source or an `sdk_factory` bridge and supply `PYKBROKER_ACCOUNT`, `PYKBROKER_TOSS_ACCOUNT_SEQ`, and `PYKBROKER_CLIENT_OPTIONS` for `create_broker("toss")`. Async SDK creation, calls, and shutdown run on an adapter-owned event loop; verification uses bridge mocks. The `toss` extra does not install vendor source, and the Toss SDK's own dry-run/live flags apply separately.

### What prevents an AI assistant from submitting live orders by default?

SDK dry-run is on by default; MCP registers mutation tools only with a valid `~/.pykbroker/limits.toml` containing `allow_orders = true` and positive `per_order_krw_cap` and `daily_krw_cap`. Omitted `dry_run` defaults to `true`, and live submission requires limits, a usage ledger, an idempotency store, and a kill switch. File deletion or changes block subsequent MCP orders; circuit breakers are optional and failed requests are not automatically retried. Users choose their own caps; read the [MCP configuration](mcp.md) and [safety primitives](safety.md).

### Is pykbroker free and can I use it commercially?

The code is provided under [Apache-2.0](https://github.com/STOA-company/pykbroker/blob/main/LICENSE), which allows commercial use, modification, and redistribution subject to its conditions. Brokerage API terms, fees, and separately obtained vendor SDK licenses require separate review. See [NOTICE](notice.md) for attribution and rewriting scope.

### Are LS, Kiwoom, NH Investment, and KB Securities supported?

As of 2026-10-01, LS is registered in `brokers/ls.py` and the factory, with **writes locked by default**. Registration does not enable trading: detailed specifications, candidate fill-price semantics, and H-LS round trips remain unverified. `experimental.ls` aliases the same module. See the support matrix for the other experimental adapters.

## Related projects

The sibling [pycex](https://github.com/Jaeminyx-Stoa/pycex) informed packaging, fixture, FastMCP, and documentation conventions.

Documentation and contribution:

- [Installation and client injection](getting-started.md), [한국어 문서 / Korean documentation](../index.md)
- [Broker support](BROKER_MATRIX.md), [Adding an adapter](BROKER_ADAPTER_RECIPE.md)
- [Safety](safety.md), [Models](api/models.md), [CLI](cli.md)
- [Execution SDK contract](execution-sdk-contract.md): documentation and types only
- [Contributing](contributing.md), [Security](security.md), [Changelog](changelog.md)

## Documentation address and AI-readable text

The planned documentation address is `https://stoa-company.github.io/pykbroker/`. This change does not publish it, and its public availability is unverified. [llms.txt](../llms.txt) contains a summary and key links; [llms-full.txt](../llms-full.txt) combines the complete Korean and English Markdown documentation as plain text. These files do not guarantee crawling or AI citations.

[한국어](../index.md)

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
  "description": "pykbroker unifies KIS, DB Securities and Toss APIs in a Python SDK and read-only-by-default MCP server. Installation, support limits and safety.",
  "inLanguage": "en"
}
</script>
