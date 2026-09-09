---
description: "Environment-based credential injection, log masking, order-ledger preservation and private security reporting for pykbroker."
lang: en
---

# Security

Supply credentials, account identifiers, and tokens through environment variables only; never hardcode them in source, fixtures, documentation, or command arguments. When injecting an authenticated client, the caller application must also read its configuration from the environment. Do not commit `.env` or token files.

The package does not copy raw requests, responses, or exception bodies into logs. Accounts are masked, for example as `****1234`, and short identifiers are fully masked. Separately review the injected vendor SDK's logging, token cache, and retry policy.

Restrict access to `limits.toml` and order-ledger files to the caller. Do not delete SQLite ledgers or change client_key to retry an ambiguous order. Setting `dry_run=False` alone does not create mandatory policies, stores, or a kill switch.

Report security issues through the repository's private vulnerability reporting feature or a private channel provided by a maintainer, without placing actual sensitive values in public issues. On exposure, revoke and reissue credentials and inspect commit history, logs, and artifacts.

[한국어](../security.md)
