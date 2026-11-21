"""Print a fingerprint of the pricing tables on OpenAI's pricing page.

    python tools/pricing_fingerprint.py page.html
    curl -s URL | python tools/pricing_fingerprint.py -

The page is server-rendered. Its pricing tables sit in Astro island `props` attributes
as HTML-escaped JSON that holds a "rows" key.
This script hashes those values and nothing else, so a change to the page around them
does not change the hash.
It never reads a number. It only says that the tables changed, and a person then updates
prices.toml by hand.

Exit 0 prints the sha256.
Exit 1 prints "page shape changed": no pricing props were found.
Standard library only, so it runs before any environment is installed.
"""

from __future__ import annotations

import hashlib
import html
import re
import sys
from pathlib import Path

# An <astro-island ...> tag, read attribute by attribute so a ">" inside a value is safe.
ISLAND = re.compile(r'<astro-island\b(?:[^>"]|"[^"]*")*?\sprops="([^"]*)"')


def pricing_props(page: str) -> list[str]:
    """Every island props value that holds a pricing table, unescaped and sorted."""
    values = (html.unescape(raw) for raw in ISLAND.findall(page))
    return sorted(v for v in values if '"rows"' in v)


def fingerprint(page: str) -> str | None:
    """The sha256 of the pricing props, or None when the page has none."""
    tables = pricing_props(page)
    if not tables:
        return None
    return hashlib.sha256("".join(tables).encode()).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: pricing_fingerprint.py PAGE.html | -", file=sys.stderr)
        return 2
    page = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text(encoding="utf-8")
    digest = fingerprint(page)
    if digest is None:
        print("page shape changed", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
