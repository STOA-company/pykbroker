# Toss B resource ownership pack (2026-09-22 UTC)

- Scope: adapter PR #3, `feat/toss-b-adapter-0922`; no live calls or deployment.
- Entry: `Toss._run` lazily constructs `_TossBridge`; `Toss.close` invokes its `aclose`.
- `_TossBridge` creates `HttpTransport` and injects it into `TossOpenApiClient`.
- The SDK treats an injected transport as externally owned (`_owns_transport=False`).
- The bridge retains `_transport` and closes it after closing the SDK client.
- `tests/test_toss_v1217.py` uses the real SDK with `httpx.MockTransport`.
- Its recorded-fixture test verifies every created HTTP client is closed by `Toss.close`.
- Removing the transport close makes that assertion fail; mutation restored.
- Same-kind search: this adapter has one transport construction and one bridge close path.
- Safety modules, order authorization and live-order configuration are unchanged.
- Verification uses `/home/quantus/coding/toss-openapi/src`; live behavior was not exercised.
