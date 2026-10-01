"""Regenerate crawler assets and set per-page HTML language/alternates.

Run manually after editing docs: python docs/_hooks/seo.py
MkDocs runs the same generator before collecting documentation files.
The committed sitemap is an offline fallback; MkDocs builds its own sitemap.
"""

import html
import re
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def site_url():
    # One scalar from the project config; MkDocs itself validates the full YAML.
    value = re.search(r"^site_url:\s*(\S+)\s*$", (ROOT / "mkdocs.yml").read_text(encoding="utf-8"), re.M).group(1)
    if urlsplit(value).scheme != "https" or not value.endswith("/"):
        raise ValueError("site_url must be an absolute HTTPS URL ending with /")
    return value


def page_url(path, base):
    relative = path.relative_to(DOCS)
    if relative.suffix == ".md":
        relative = relative.parent if relative.name == "index.md" else relative.with_suffix("")
        suffix = relative.as_posix().strip(".").strip("/")
        return base + (suffix + "/" if suffix else "")
    return base + relative.as_posix()


def absolute_link(target, source, base):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return target
    if not parsed.path:
        return page_url(source, base) + target
    destination = (source.parent / parsed.path).resolve()
    if not destination.is_relative_to(DOCS):
        raise ValueError(f"Documentation link escapes docs: {source.name}: {target}")
    url = page_url(destination, base)
    return url + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")


def document_text(path, base):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S)
    # Source pages use inline Markdown links. Keep all prose, tables, code and
    # warnings intact, while replacing each relative link with a canonical URL.
    return re.sub(
        r"(\[[^\]\n]*\]\()([^\s)]+)(\))",
        lambda m: m[1] + absolute_link(m[2], path, base) + m[3],
        text,
    ).strip()


def generate():
    base = site_url()
    pages = sorted(DOCS.rglob("*.md"), key=lambda p: p.relative_to(DOCS).as_posix())
    sections = []
    summaries = []
    for path in pages:
        body = document_text(path, base)
        title = re.search(r"^# (.+)$", body, re.M).group(1)
        sections.append(f"Source: {page_url(path, base)}\n\n{body}")
        description = re.search(r'^description: "(.*)"$', path.read_text(encoding="utf-8"), re.M).group(1)
        summaries.append(f"- [{title}]({page_url(path, base)}): {description}")
    prefix = (
        "# pykbroker\n\n"
        "> Apache-2.0 Python SDK and read-only-by-default MCP server for Korean broker Open APIs.\n\n"
        "한국어 기본 문서와 영어 번역 / Korean documentation with English translations.\n"
        "KIS, DBFI and Toss are implemented adapters with offline mock/fixture validation only.\n"
        "Real APIs, paper accounts and live trading have not been validated. LS is registered\n"
        "with default-locked writes and UNVERIFIED candidate fill fields; Kiwoom is registered; experimental NH/KB\n"
        "adapters remain outside the factory. See the broker support matrix.\n"
        "Dry-run blocks order/cancellation transmission; it does not simulate successful orders.\n"
        "Vendor source SDKs and authentication must be supplied separately.\n"
        "The execution SDK contains contracts only, without an execution engine.\n"
        "Canonical URLs below are planned publication targets; public availability is unverified.\n"
        "These files do not publish the site or guarantee crawling or AI citations.\n\n"
    )
    (DOCS / "llms.txt").write_text(
        prefix
        + "[Source repository](https://github.com/STOA-company/pykbroker)\n\n"
        + f"[Full documentation text]({base}llms-full.txt)\n\n## Documentation\n\n"
        + "\n".join(summaries)
        + "\n",
        encoding="utf-8",
    )
    (DOCS / "llms-full.txt").write_text(prefix + "\n\n-----\n\n".join(sections) + "\n", encoding="utf-8")
    (DOCS / "robots.txt").write_text(
        "# Planned project-site asset. Crawlers consult /robots.txt at the host root;\n"
        "# /pykbroker/robots.txt alone does not set host-wide crawling policy.\n"
        f"User-agent: *\nAllow: {urlsplit(base).path}\n\nSitemap: {base}sitemap.xml\n",
        encoding="utf-8",
    )
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ET.register_namespace("", namespace)
    root = ET.Element(f"{{{namespace}}}urlset")
    for path in pages:
        node = ET.SubElement(root, f"{{{namespace}}}url")
        ET.SubElement(node, f"{{{namespace}}}loc").text = page_url(path, base)
    ET.indent(root)
    ET.ElementTree(root).write(DOCS / "sitemap.xml", encoding="utf-8", xml_declaration=True)


def on_pre_build(config):
    generate()


def on_post_page(output, page, config):
    source = DOCS / page.file.src_uri
    english = source.is_relative_to(DOCS / "en")
    relative = source.relative_to(DOCS / "en" if english else DOCS)
    base = config["site_url"]
    alternatives = {"ko": DOCS / relative, "en": DOCS / "en" / relative}
    links = "\n".join(
        f'<link rel="alternate" hreflang="{language}" href="{html.escape(page_url(path, base), quote=True)}">'
        for language, path in alternatives.items()
        if path.is_file()
    )
    output = re.sub(r'(<html\b[^>]*\blang=")[^"]*(")', rf"\g<1>{'en' if english else 'ko'}\2", output, count=1)
    return output.replace("</head>", links + "\n</head>", 1)


if __name__ == "__main__":
    generate()
