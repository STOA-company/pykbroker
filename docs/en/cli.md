---
description: "Read-only pykbroker CLI commands for quotes, balances, positions, order status and rules, with error exit-code behavior."
lang: en
---

# CLI

```bash
pykbroker --help
pykbroker --version
pykbroker brokers
pykbroker --broker kis quote 005930
pykbroker --broker dbfi balance
pykbroker --broker dbfi positions
pykbroker --broker kis orders --symbol 005930
pykbroker --broker kis rules 005930
```

Follow the [environment-variable guide](getting-started.md) for authentication. The CLI is read-only. Configuration and vendor errors return exit code 2 with an error message that excludes account and credential values.

[한국어](../cli.md)
