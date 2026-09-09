---
description: "Connect pykbroker-mcp to Claude Desktop with JSON configuration, authentication environment injection, read-only checks and troubleshooting."
lang: en
---

# Connect Korean brokerage tools to Claude Desktop

pykbroker provides a read-only-by-default stdio server through `pykbroker-mcp`. This guide uses a source checkout and requires no PyPI publication or MCP registry entry. Configuration locations and restart steps follow the [official MCP local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## 1. Install and prepare authentication

Create a virtual environment with Python 3.11 or later in the checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,kis]'
```

On Windows use `.venv\Scripts\python.exe`. The `kis` extra installs supporting dependencies only. Obtain compatible `Pykis` source or configure an authenticated client according to [getting started](getting-started.md) before real queries.

Arrange for an external secret manager to inject `PYKBROKER_ACCOUNT` and the JSON object `PYKBROKER_CLIENT_OPTIONS` into the **server process environment**. GUI applications may not inherit your terminal environment. If necessary, change `command` and `args` to start `pykbroker-mcp` through your secret manager's launcher. pykbroker does not provide that launcher or token management. Never paste actual accounts or keys into example configuration, conversations, or command history.

## 2. Configure claude_desktop_config.json

Open Settings → Developer → Edit Config in Claude Desktop and merge a `pykbroker` entry into the existing `mcpServers` object. Preserve other server entries.

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

This macOS template uses an **absolute path**. Replace `/absolute/path/to/pykbroker` with the real checkout location. Do not assume that `~` or shell variables expand inside `command`.

```json
{
  "mcpServers": {
    "pykbroker": {
      "command": "/absolute/path/to/pykbroker/.venv/bin/pykbroker-mcp",
      "args": [],
      "env": {
        "PYKBROKER_BROKER": "kis"
      }
    }
  }
}
```

A Windows command example is `C:\\absolute\\path\\to\\pykbroker\\.venv\\Scripts\\pykbroker-mcp.exe`; JSON backslashes must be doubled. Supply the account and authentication options through the process environment prepared above. This JSON alone does not authenticate the server.

## 3. Verify read-only tools

Fully quit and restart Claude Desktop. Check for `get_quote`, `get_balance`, `get_positions`, `list_orders`, and `get_rules`, then request “Get the current Samsung Electronics 005930 quote through KIS.” `get_rules` returns basic rules and does not know tick sizes for every product.

With no `~/.pykbroker/limits.toml`, `place_order` and `cancel_order` should be **absent**. An account with existing order authorization differs from this default; review [MCP safety configuration](mcp.md) first. This query guide does not create an order authorization file.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Server missing / ENOENT | Valid JSON, actual absolute executable path, installed virtual environment, full application restart |
| Tools visible but queries fail | Server environment injection, compatible vendor-source import path, authenticated client settings |
| Offline or authentication failure | Errors must be returned instead of real quotes; use `python examples/offline_quote.py` for a synthetic offline demonstration |
| Order tools missing | Expected default behavior; queries do not require order authorization |

Logs are under macOS `~/Library/Logs/Claude/` or Windows `%APPDATA%\Claude\logs`. Inspect account/token content and vendor SDK logging before sharing logs. Actual Claude Desktop connectivity was not validated in this sandbox; offline tool registration/invocation tests are separate evidence.

[한국어](../claude-desktop.md)
