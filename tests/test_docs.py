"""The user docs keep up with the code: every setting, fault and outcome has its place."""

import re
from pathlib import Path

import pytest

from faultline_sdk import config, runner

DOCS = Path(__file__).parents[1] / "docs"


def page(name: str) -> str:
    return (DOCS / name).read_text()


def mentioned(text: str, word: str) -> bool:
    return f"`{word}`" in text or f'`"{word}"`' in text


@pytest.mark.design("docs.every_setting")
def test_every_setting_is_documented() -> None:
    text = page("configuration.md")
    keys = (config.AGENT_KEYS | config.TEST_KEYS | config.MOCK_KEYS | config.FAULT_KEYS) - {
        "mock",
        "fault",
    }
    missing = sorted(k for k in keys if not mentioned(text, k))
    assert not missing, f"settings with no entry in docs/configuration.md: {missing}"


def test_every_setting_not_built_yet_is_listed() -> None:
    text = page("configuration.md").split("## Not supported yet", 1)[1]
    later = config.AGENT_LATER | config.MOCK_LATER | config.FAULT_LATER
    names = {k for k in later if k != "prices"} | {"[agent.prices]"}
    missing = sorted(k for k in names if not mentioned(text, k))
    assert not missing, f"not-yet settings missing from the list: {missing}"


def test_every_fault_and_outcome_is_documented() -> None:
    concepts = page("concepts.md")
    assert all(mentioned(concepts, f) for f in config.FAULTS)
    assert all(outcome in concepts for outcome in runner.OUTCOMES)


def test_exit_codes_match_the_runner() -> None:
    cli = page("cli.md")
    for code in (runner.OK, runner.MISTAKE, runner.INFRA):
        assert f"| {code} |" in cli


def test_every_page_is_linked_from_the_index_and_every_link_works() -> None:
    index = page("index.md")
    links = set(re.findall(r"\]\(([a-z-]+\.md)\)", index))
    pages = {p.name for p in DOCS.glob("*.md")} - {"index.md"}
    assert links == pages, f"index links {sorted(links)}, pages {sorted(pages)}"
    for name in pages:
        for target in re.findall(r"\]\(([a-z-]+\.md)\)", page(name)):
            assert (DOCS / target).exists(), f"{name} links to missing {target}"
