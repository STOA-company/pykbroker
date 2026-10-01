---
description: "Install pykbroker from source, prepare KIS/DB/Toss vendor SDKs, configure authentication environment variables and inject clients."
lang: en
---

# Getting started

pykbroker requires Python 3.11 or later and Pydantic v2. Importing the package does not authenticate, issue tokens, or open sockets.

## Installation

```bash
# Current, unpublished source checkout:
pip install -e '.[mcp,dev]'
# Only after a distribution is published on PyPI:
# pip install pykbroker
# pip install 'pykbroker[mcp]'
```

| Extra | What it installs |
| --- | --- |
| `kis` | Supporting runtime dependencies for Pykis |
| `dbfi` | Supporting runtime dependencies for Pydb |
| `toss` | HTTP and settings dependencies for the Toss source SDK |
| `mcp` | FastMCP 1.x |
| `dev` | pytest, pytest-asyncio, ruff, build |
| `docs` | MkDocs Material |

**Vendor source distributions are not included.** The referenced KIS source uses `Pykis._pykis` and `Pykis.base`; DB uses `Pydb.pydbfi`. Installation metadata in those revisions does not match these capitalized import paths, so the extras do not select an arbitrary similarly named PyPI package. The `kis` and `dbfi` extras install supporting dependencies only.

Place reviewed source trees named `Pykis/` and `Pydb/` under a separate parent directory and add that parent to Python's import path, or inject objects as below. See [attribution](notice.md) for the referenced revisions. Check vendor source licenses and access rights separately. `toss_openapi` also requires a compatible source SDK or injected bridge. Connecting compatible installable distributions remains a TODO.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `PYKBROKER_BROKER` | `kis`, `dbfi`, or `toss`; `ls` is registered but locked and requires an injected client; CLI/MCP default to `kis` |
| `PYKBROKER_ACCOUNT` | Caller-owned account identifier; KIS requires the vendor's actual account format |
| `PYKBROKER_CLIENT_OPTIONS` | JSON object of authentication constructor arguments |
| `PYKBROKER_TOSS_ACCOUNT_SEQ` | Toss-issued accountSeq, distinct from the account number |

Do not put real values in source, command history, or documentation. Arrange for an external secret manager to inject the environment. The CLI does not accept account identifiers or keys as arguments. The SDK provides no policy for storing or refreshing authentication options.

## Inject an authenticated client

```python
from pykbroker import KIS, DBFI

# authenticated_client and vendor_api are supplied by the caller.
# KIS: client.account(caller account), api.get_price_domestic(...),
# api.get_quote_domestic(...), etc.
broker = KIS(account=caller_account, client=authenticated_client, api=vendor_api)
quote = broker.get_quote("005930")
broker.close()

# DBFI accepts a Pydb DBFI object directly; no api helper is required.
broker = DBFI(account=caller_account, client=authenticated_db_client)
```

The caller owns and closes injected clients. For clients created through `client_factory`, the adapter calls `close()` when supported. KIS selects the account with `client.account(account)`. DB credentials are bound to the account by the vendor; the caller must ensure that the supplied identifier and client refer to the same account.

Toss accepts `sdk_factory`. Construction, use, and shutdown run on an instance-owned event loop. Async applications can call the synchronous API through `await asyncio.to_thread(broker.get_quote, "005930")`; complete `close()` in the same way.

## Trading scope and unsupported features

The broker implementations cover integer-share KR/US cash equities. Derivatives, leverage, portfolio rebalancing, automatic retries, and an execution engine are not included. `get_rules()` returning `tick=None` means the price band or product type is unknown. Unsupported features raise `NotSupportedError`.

Construct KIS/DB US adapters with `market="US"` and `venue="NASDAQ" | "NYSE" | "AMEX"`. US balances require a `cash_provider(client_or_scope) -> Decimal` callback supplying USD cash available for orders. The library does not choose an arbitrary symbol or account type. Inject KRW valuation for US order limits through `OrderSafety.valuation`.

Supply all vendor clients with **order retries disabled**. This package does not retry failed submissions or cancellations; callers must inspect retries inside the vendor SDK separately.

[한국어](../getting-started.md)
