"""The limits page answers "will it work for my agent" first, then groups the rest by use case."""

import re
from pathlib import Path

DOCS = Path(__file__).parents[1] / "docs"
FIT = "Will Scrimmage work for your agent?"
SIX = [
    "OpenAI only.",
    "LangChain only.",
    "httpx only.",
    "No streaming replies.",
    "Rules match words, not meaning.",
    "Install from git.",
]


def lines() -> list[str]:
    return (DOCS / "limits.md").read_text().split("\n")


def fit_section() -> list[str]:
    text = lines()
    start = text.index(f"## {FIT}")
    end = next(i for i in range(start + 1, len(text)) if text[i].startswith("## "))
    return text[start:end]


def test_the_page_opens_with_will_scrimmage_work_for_your_agent() -> None:
    headings = [line for line in lines() if line.startswith("#")]
    assert headings[0] == "# Limits of this version"
    assert headings[1] == f"## {FIT}"


def test_the_fit_section_holds_the_six_in_order() -> None:
    titles = re.findall(r"^- \*\*([^*]*)\*\*", "\n".join(fit_section()), re.M)
    assert titles == SIX


def test_every_limit_is_under_a_use_case_heading() -> None:
    heading = ""
    outside = []
    for line in lines():
        if line.startswith("#"):
            heading = line
        elif line.startswith("- **"):
            title = re.match(r"- \*\*([^*]*)\*\*", line)
            assert title
            above = heading in ("# Limits of this version", f"## {FIT}")
            if above and title.group(1) not in SIX:
                outside.append(line[:60])
    assert not outside, f"a limit is above the first use case heading: {outside}"
    wanted = [
        "## A function or a script agent",
        "## An agent inside a web service",
        "## In Docker Compose",
        "## In a Kubernetes cluster",
        "## General",
    ]
    present = [line for line in lines() if line in wanted]
    assert present == wanted


def test_each_of_the_six_links_to_its_detail() -> None:
    bullets = "\n".join(fit_section()).split("\n- ")[1:]
    assert len(bullets) == len(SIX)
    for bullet in bullets:
        link = re.search(r"\]\(([a-z-]+\.md)(#[a-z0-9-]+)?\)", bullet)
        assert link, f"no link in: {bullet[:40]}"
        target = DOCS / link.group(1)
        assert target.exists(), link.group(1)
        if link.group(2):
            anchor = link.group(2)[1:]
            heads = [
                re.sub(r"[^a-z0-9 -]", "", h.lstrip("#").strip().lower()).replace(" ", "-")
                for h in target.read_text().split("\n")
                if h.startswith("#")
            ]
            assert anchor in heads, f"{link.group(0)} has no heading"
