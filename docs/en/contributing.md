---
description: "Contribute to pykbroker with safety-change reviews, offline tests and coordinated adapter documentation and license updates."
lang: en
---

# Contributing

Bug reports and pull requests are welcome. One designated maintainer has merge authority. Safety-related changes require an additional reviewer other than the author before that maintainer merges them.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,mcp]'
pytest
ruff check src tests examples
ruff format --check src tests examples
```

All tests must be offline. Vendor response fixtures must contain no real account or credential values. Order/cancellation changes must verify exact request arguments, dry-run, caps, duplicates, and timeouts. Update documentation, tests, capabilities, and the support matrix together for new adapters. Unlocking an unverified specification requires documentation and independent verification evidence.

Python sources live in `src/`, Pydantic models in `models/`, and JSON fixtures in `tests/fixtures/<broker>/`. Like pycex, the project uses hatchling, ruff, pytest-asyncio, and MkDocs Material. Do not add a request executor, strategy execution, or company-specific storage.

You must have the right to contribute under Apache-2.0. If you refer to existing code, record the source and actual reuse scope in NOTICE.

## Updating documentation and search assets

When editing Korean Markdown, update the matching `docs/en/` translation and both page descriptions. pycex has no multilingual configuration, so this repository uses `docs/en/` with Korean/English navigation. Register new pages in both navigation trees.

```bash
pip install -e '.[docs]'
python docs/_hooks/seo.py
mkdocs build --strict
```

The generator refreshes `llms.txt`, `llms-full.txt`, `robots.txt`, and an offline fallback `sitemap.xml` from the actual complete Markdown documentation. The pre-build hook runs the same generator; MkDocs core creates the built site's sitemap. The page hook sets the HTML language and translation `hreflang` links. Planned addresses come from `site_url` in `mkdocs.yml`; regenerate and inspect the assets if it changes. These commands do not publish documentation.

A project-path `robots.txt` does not replace the host-root robots policy. Before publication, separately check public availability, actual MkDocs rendering, desktop/mobile navigation, themes, and table overflow.

[한국어](../contributing.md)
