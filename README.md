# pykbroker

English | [한국어](README.ko.md)

pykbroker is an Apache-2.0 Python library that unifies Korean brokerage Open APIs (KIS, DB Securities, Toss Securities, Kiwoom, and NH Investment & Securities) behind one common interface and a read-only-by-default MCP server.

| Adapters | Offline tests | Python |
| --- | --- | --- |
| **5** — KIS, DBFI, Toss, Kiwoom, NH | Fake-response contracts and mutation checks | **3.11+** |

Status as of the 2026-10-01 registry. The adapter count is the number of entries in the package's `BROKERS` registry, and tests run on mocks and synthetic fixtures. **No live API, demo-account, or real-money trading validation has been performed.** See the [support matrix](docs/BROKER_MATRIX.md) for exact coverage. This is an early alpha; install from source, as it is not yet on PyPI.

## 30-second example: a KIS quote

Install from a repository checkout:

```bash
pip install -e '.[mcp]'
```

Prepare the broker's authenticated client and environment variables as described in [Getting started](docs/getting-started.md), then:

```python
from pykbroker import create_broker

with create_broker("kis") as broker:
    quote = broker.get_quote("005930")
    print(quote.symbol, quote.last, quote.bid, quote.ask)
```

Set `PYKBROKER_ACCOUNT` to the caller's account identifier and `PYKBROKER_CLIENT_OPTIONS` to the vendor constructor settings as JSON. Even quotes may require authentication. The KIS, DB, and Toss vendor SDKs must be installed separately; the extras only add supporting dependencies. The 30-second example assumes that setup is done.

To try it without keys, run `python examples/offline_quote.py`. It uses a synthetic response, not a real quote. Once a release is published, `pip install pykbroker` or `pip install 'pykbroker[mcp]'` will work.

## How it works

```text
caller → StockBroker / BrokerAdapter contract → broker adapter → vendor SDK
                       ↑                        │
                   MCP / CLI                    └ orders: safety gate → vendor send
```

Reads return common models. Orders and cancels pass through **dry run → per-order and daily limits → idempotency key → kill switch → optional circuit breaker → adapter send**. The caller supplies the limits, account, credentials, and state providers. Orders are blocked by default.

## Layout

| Module | What it does |
| --- | --- |
| `base` | Common contract for reads, orders, cancels, and shutdown |
| `brokers` | Registered KIS, DBFI, Toss, Kiwoom, LS, and NH adapters; validated by offline contract tests |
| `experimental` | LS compatibility path, KB skeleton, and legacy mappings; the old Kiwoom balance call stays blocked and orders stay locked |
| `safety` | Caller-injected limits, idempotency, dry run, and stop decisions |
| `mcp` | Read-only tools by default; order tools are registered only through configuration |

## FAQ

### How do I read quotes and balances from the KIS (Korea Investment & Securities) API in Python?

Prepare a compatible Pykis source and authentication settings, then call `get_quote("005930")` or `get_balance()` on `create_broker("kis")`. You can also inject an already authenticated client and API helper into the `KIS` constructor. See [Getting started](docs/getting-started.md) and the runnable [KIS balance example](examples/kis_balance_check.py).

### Can I connect Korean broker APIs to Claude Desktop as MCP tools?

Install the `mcp` extra and run the stdio MCP server with `pykbroker-mcp`. The default tools are `get_quote`, `get_balance`, `get_positions`, `list_orders`, and `get_rules`; reads still need the vendor SDK and authentication. The [Claude Desktop setup example](examples/claude_desktop_mcp_setup.md) has the command path and JSON configuration. The Claude Desktop connection itself has not been verified in this environment.

### Can I test with a demo account or a dry run instead of real orders?

The default dry run blocks order and cancel sends with `DryRunError`; it never fabricates a simulated fill or a successful order. The key-free examples and tests use mocks and synthetic fixtures, so their quote data is not real either. Demo-account access depends on the vendor client settings; there is no common demo-account switch, and no demo account has been validated.

### Does it support the DB Securities Open API?

Yes. `create_broker("dbfi")` or `DBFI` wraps a Pydb client. KR and US reads, order translation, and single-order cancel are covered by offline contract tests; the live API has not been validated. For US balances the caller must supply orderable cash through `cash_provider`, so check the [support matrix](docs/BROKER_MATRIX.md) first.

### How do I connect the Toss Securities API, and what do I need?

Prepare a compatible `toss_openapi` source SDK or an `sdk_factory` bridge, and pass `PYKBROKER_ACCOUNT`, `PYKBROKER_TOSS_ACCOUNT_SEQ`, and `PYKBROKER_CLIENT_OPTIONS` to `create_broker("toss")` through the environment. The adapter creates, calls, and closes the async SDK on its own event loop; validation covers a mock bridge. The `toss` extra does not install the vendor source, and the Toss SDK's own dry-run and live flags apply separately.

### What stops an AI from sending real orders?

The SDK runs in dry run by default. The MCP server registers order and cancel tools only when a valid `~/.pykbroker/limits.toml` has `allow_orders = true` and positive `per_order_krw_cap` and `daily_krw_cap`. `dry_run` defaults to `true` when omitted, and a real send needs limits, a usage store, an idempotency store, and a kill switch. Deleting or changing the settings blocks later MCP orders. The circuit breaker is optional, and nothing retries automatically. You set the numeric limits yourself; see [MCP settings](docs/mcp.md) and [safety primitives](docs/safety.md).

### Is pykbroker free, and can I use it commercially?

The code is licensed under [Apache-2.0](LICENSE), so commercial use, modification, and distribution are allowed under its terms. Check each broker's API terms and fees, and the licenses of the vendor SDKs you install separately. Sources and rewrite scope are listed in [NOTICE](NOTICE).

### Are LS Securities, Kiwoom, NH, and KB supported?

The old `experimental.kiwoom.get_balance` is blocked on call because it returned estimated deposit assets as cash. Use `create_broker("kiwoom")` or `pykbroker.brokers.kiwoom.Kiwoom` instead.

Kiwoom, LS, and NH are registered in the factory and their contracts are checked against fake responses. LS writes work only on a verified demo instance; **orders and cancels on a live binding are locked**. It needs a client with an explicit host, and the demo-endpoint contract and detailed spec are not yet confirmed. While locked, quantity, filled quantity, and fill price are `None`, and `fill_price` stays empty until the average-fill definition is confirmed. `experimental.ls` is a compatibility path to the same module. NH implements KRX and NXT cash limit orders and single cancels, with **live orders locked by default in the SDK**; order-number-to-ledger linking and real API or demo round trips are not yet validated. KB alone is a send-free skeleton outside the factory.

## Related projects

Packaging, fixtures, FastMCP, and docs structure follow the sister project [pycex](https://github.com/STOA-company/pycex).

The product that runs on this library: [Quantus Q](https://www.quantus.kr/q).

Docs and contributing:

- [Install and client injection](docs/getting-started.md), [English documentation](docs/en/index.md)
- [Adapter support matrix](docs/BROKER_MATRIX.md), [Adding a new adapter](docs/BROKER_ADAPTER_RECIPE.md)
- [Safety primitives](docs/safety.md), [Models](docs/api/models.md), [CLI](docs/cli.md)
- [Execution SDK contract](docs/execution-sdk-contract.md) (docs and types only)
- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), [Changelog](CHANGELOG.md)
