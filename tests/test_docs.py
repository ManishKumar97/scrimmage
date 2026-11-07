"""The user docs keep up with the code: every setting, fault and outcome has its place."""

import re
from pathlib import Path

import pytest

from derail_sdk import config, judge, mcp_gate, runner

DOCS = Path(__file__).parents[1] / "docs"


def page(name: str) -> str:
    return (DOCS / name).read_text()


def mentioned(text: str, word: str) -> bool:
    return f"`{word}`" in text or f'`"{word}"`' in text


@pytest.mark.design("docs.every_setting")
def test_every_setting_is_documented() -> None:
    text = page("configuration.md")
    keys = (config.AGENT_KEYS | config.TEST_KEYS | config.MOCK_KEYS | config.MODEL_FAULT_KEYS) - {
        "mock",
        "fault",
    }
    missing = sorted(k for k in keys if not mentioned(text, k))
    assert not missing, f"settings with no entry in docs/configuration.md: {missing}"


@pytest.mark.design("docs.web_settings")
def test_every_web_setting_and_real_form_is_documented() -> None:
    text = page("configuration.md")
    keys = config.WEB_MOCK_KEYS | config.WEB_FAULT_KEYS
    missing = sorted(k for k in keys if not mentioned(text, k))
    assert not missing, f"web settings with no entry in docs/configuration.md: {missing}"
    web = page("web-requests.md")
    for form in ("`http:orders.acme.internal`", " POST`", " POST /refunds`"):
        assert form in web


@pytest.mark.design("docs.mcp")
def test_mcp_settings_faults_and_labels_are_documented() -> None:
    text = page("mcp.md")
    for word in ("timeout", "error", "empty", "mcp:docs/search", "mcp:search"):
        assert mentioned(text, word), word
    assert all(f"`{method}`" in text for method in mcp_gate.PASSES)
    assert "`mcp:<server>/<tool>`" in page("concepts.md")
    assert "`mcp:" not in page("configuration.md").split("## Coming in later versions")[1]


@pytest.mark.design("docs.scripts")
def test_the_script_shape_its_children_and_its_stopping_are_documented() -> None:
    text = page("scripts.md")
    for word in ("{question}", "children", "DERAIL_ANSWER_FILE", "process", "other_python"):
        assert f"`{word}`" in text or f"`{word} = true`" in text, word
    assert "`command`" in page("configuration.md") and "`derail-run" in page("cli.md")
    assert "`command`" not in page("configuration.md").split("## Coming in later versions")[1]
    assert "mcp 1.x, started with your environment" not in page("limits.md")


@pytest.mark.design("docs.judging")
def test_the_rules_the_table_and_the_endings_are_documented() -> None:
    text = page("judging.md")
    for word in sorted(config.RULE_KEYS) + runner.HEAD[3:-1]:
        assert mentioned(text, word), word
    for result in (judge.PASS, judge.FAIL, judge.NO_RESULT):
        assert f"`{result}`" in text
    assert all(f"| {outcome}" in text for outcome in runner.OUTCOMES)
    assert judge.EMPTY in text
    assert "not a promise about the next one" in text
    assert "No judging" not in page("limits.md")
    assert "later version" not in text


def test_every_fault_and_outcome_is_documented() -> None:
    concepts = page("concepts.md")
    assert all(mentioned(concepts, f) for f in config.FAULTS)
    missing = [o for o in runner.OUTCOMES if f"| {o} |" not in concepts]
    assert not missing, f"outcomes with no row in docs/concepts.md: {missing}"


def test_exit_codes_match_the_runner() -> None:
    cli = page("cli.md")
    for code in (runner.OK, runner.ROW_FAILED, runner.MISTAKE, runner.INFRA):
        assert f"| {code} |" in cli


def test_every_page_is_linked_from_the_index_and_every_link_works() -> None:
    index = page("index.md")
    links = set(re.findall(r"\]\(([a-z-]+\.md)\)", index))
    pages = {p.name for p in DOCS.glob("*.md")} - {"index.md"}
    assert links == pages, f"index links {sorted(links)}, pages {sorted(pages)}"
    for name in pages:
        for target in re.findall(r"\]\(([a-z-]+\.md)\)", page(name)):
            assert (DOCS / target).exists(), f"{name} links to missing {target}"


@pytest.mark.design("docs.model_name")
def test_a_fault_on_one_model_and_its_limits_are_documented() -> None:
    assert "| `model` | On a model call" in page("configuration.md")
    guide = page("model-faults.md")
    for word in ("gpt-4.1", "gpt-4o-mini-2024-07-18", "unreadable_model"):
        assert word in guide, word
    limits = page("limits.md")
    assert "**Two calls to one model.**" in limits
    assert "**A model name is compared whole.**" in limits
    assert "**A fault with `model` needs a name it can read.**" in limits
    assert "**Two `model` keys in one body.**" in limits
    for text in (guide, limits):
        assert "the row can pass, and nothing warns" in text
        assert "not the name a reply or a dashboard shows" in text
        assert "sent from a generator" in text
        assert "the openai library takes" in text.lower()
        assert '`""` under `[agent.prices]`, or set `max_cost_usd = "none"`' in text
        assert "any model call with no name, such as a Responses call on a stored prompt" in text
        assert "streamed, multipart" not in text
    assert "a character that does not print" in guide
    assert '"" = {input = 2.0, output = 8.0}' in guide
    assert 'The key `""` prices a call that sends no model name.' in page("configuration.md")
    assert "with the reply streamed or not" in guide
    safety = page("safety.md")
    assert "`unreadable_model`" in safety and "- A chat request body" in safety
    assert "- A request body Derail cannot read" not in safety
    assert "`model`" not in page("configuration.md").split("## Coming in later versions")[1]


@pytest.mark.design("docs.cached_input")
def test_cached_input_prices_and_their_default_are_documented() -> None:
    text = page("configuration.md")
    assert all(mentioned(text, key) for key in config.PRICE_KEYS)
    assert "cached tokens cost the full `input` price" in text
    guide = page("model-faults.md")
    for word in ("prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens"):
        assert word in guide, word
    assert "`tokens.cached`" in guide and "the safe overestimate" in guide
    assert "**Cached input on OpenAI only.**" in page("limits.md")
