# ruff: noqa: E501
"""Create an offline, self-contained HTML reading copy of the Markdown contract."""

import re
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "docs/execution-sdk-contract.md").read_text()
content = MarkdownIt("commonmark").enable("table").render(source)
links = []


def heading(match):
    number = len(links) + 1
    text = match.group(2)
    anchor = f"section-{number}"
    links.append(f'<a href="#{anchor}">{text}</a>')
    return f'<h{match.group(1)} id="{anchor}">{text}</h{match.group(1)}>'


content = re.sub(r"<h([12])>(.*?)</h\1>", heading, content)
content = content.replace("<table>", '<div class="table-scroll" tabindex="0"><table>').replace(
    "</table>", "</table></div>"
)
html = """<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>pykbroker · 실행 SDK 계약</title>
<style>
:root{color-scheme:light dark;--bg:#f5f7fb;--fg:#17243b;--panel:#fff;--line:#d7dfeb;--accent:#234ac7}
@media(prefers-color-scheme:dark){:root{--bg:#101722;--fg:#e3e9f3;--panel:#192536;--line:#40506a;--accent:#a5baff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,sans-serif}
header,main{max-width:1060px;margin:auto;padding:24px}header{padding-bottom:0}header p{margin:0}
small{opacity:.8}nav{display:flex;flex-wrap:wrap;gap:8px 18px;margin:20px 0;padding:18px;background:var(--panel);border-radius:10px}
a{color:var(--accent);text-underline-offset:3px}nav a{font-size:14px}h1{font-size:2rem;line-height:1.3}h2{margin-top:2.7rem;scroll-margin-top:24px;font-size:1.4rem}
main{overflow-wrap:anywhere}pre{background:var(--panel);padding:18px;overflow:auto;border:1px solid var(--line);border-radius:8px}
code{font-size:.9em}table{border-collapse:collapse;min-width:540px;width:100%;font-size:14px}th,td{padding:12px;text-align:left;border:1px solid var(--line);vertical-align:top}th{background:var(--panel)}
.table-scroll{overflow:auto;max-width:100%;margin:20px 0}.banner{padding:16px;border-left:4px solid var(--accent);background:var(--panel)}
@media(max-width:600px){header,main{padding:16px}h1{font-size:1.7rem}nav{display:block}nav a{display:block;margin:8px 0}}
</style>
<header><p><strong>pykbroker</strong> · 한국 증권사 통합 SDK</p><small>Markdown 정본의 오프라인 검토본 · 외부 리소스 없음</small>
<div class="banner">계약과 타입만 제공합니다. 실행기·디스패치·재개·자동 주문 구현은 포함하지 않습니다.</div>
<nav aria-label="목차">NAV</nav></header><main>BODY</main></html>"""
html = html.replace("NAV", "\n".join(links)).replace("BODY", content)
(ROOT / "docs/execution-sdk-contract.html").write_text(html)
print(f"Rendered {len(links)} anchors from {len(source)} Markdown characters")
