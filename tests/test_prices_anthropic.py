"""The Anthropic rows of the shipped price file, pinned as read from Anthropic's pricing page and
fast mode page on 25 November 2026. A rate that changes by a cent must fail here."""

import pytest

from config_cases import BAD
from conftest import Project
from scrimmage import cli, outcomes, prices, provider_anthropic
from scrimmage.prices import Rates


def row(
    input: float,
    output: float,
    read: float,
    write_5m: float,
    write_1h: float,
    **more: object,
) -> Rates:
    return Rates(
        input,
        output,
        read,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        **more,  # type: ignore[arg-type]
    )


# input, output, cache read, 5 minute write, 1 hour write, each as the page's model table lists it
PAGE = {
    "claude-fable-5-2": row(
        8.0,
        40.0,
        0.20,
        10.0,
        16.0,
        tiers=(("fast", row(16.0, 80.0, 0.40, 20.0, 32.0)),),
    ),
    "claude-mythos-5-2": row(
        8.0,
        40.0,
        0.20,
        10.0,
        16.0,
        tiers=(("fast", row(16.0, 80.0, 0.40, 20.0, 32.0)),),
    ),
    "claude-fable-5-1": row(10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": row(10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-fable-5": row(10.0, 50.0, 1.0, 12.5, 20.0),
    "claude-mythos-5": row(10.0, 50.0, 1.0, 12.5, 20.0),
    "claude-opus-5-5": row(
        4.0,
        20.0,
        0.20,
        5.0,
        8.0,
        tiers=(("fast", row(8.0, 40.0, 0.40, 10.0, 16.0)),),
    ),
    "claude-opus-5": row(
        5.0,
        25.0,
        0.50,
        6.25,
        10.0,
        tiers=(("fast", row(10.0, 50.0, 1.0, 12.5, 20.0)),),
    ),
    "claude-opus-4-8": row(
        5.0,
        25.0,
        0.50,
        6.25,
        10.0,
        tiers=(("fast", row(10.0, 50.0, 1.0, 12.5, 20.0)),),
    ),
    "claude-opus-4-7": row(5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-6": row(5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-5": row(5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-sonnet-5-5": row(2.0, 10.0, 0.10, 2.5, 4.0),
    "claude-sonnet-5": row(2.0, 10.0, 0.20, 2.5, 4.0),
    "claude-sonnet-4-6": row(3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-sonnet-4-5": row(3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-5-5": row(
        0.10,
        0.50,
        0.01,
        0.125,
        0.20,
        above=100000,
        long=row(0.50, 2.50, 0.05, 0.625, 1.0),
    ),
    "claude-haiku-4-5": row(1.0, 5.0, 0.10, 1.25, 2.0),
}


@pytest.mark.design("prices.shipped_file")
def test_the_anthropic_section_holds_the_rows_read_from_the_page() -> None:
    section = prices.shipped().section("anthropic")
    assert section is not None
    assert dict(section.models) == PAGE
    assert section.as_of == "2026-11-25"
    assert section.url == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert section.fees == (("web_search_request", 0.01),)


@pytest.mark.design("prices.shipped_file")
def test_a_retired_model_is_not_priced() -> None:
    names = dict(prices.shipped().models("anthropic"))
    for retired in ("claude-opus-4-1", "claude-opus-4", "claude-sonnet-4"):
        assert retired not in names


@pytest.mark.design("prices.shipped_file")
def test_a_dated_snapshot_costs_its_familys_row() -> None:
    found = provider_anthropic.price("claude-opus-4-5-20251101", {})
    assert found is not None and found.input == 5.0 and found.name == "claude-opus-4-5-20251101"


@pytest.mark.design("prices.shipped_file")
def test_a_call_over_100000_tokens_on_haiku_5_5_settles_at_the_long_rates() -> None:
    rates = provider_anthropic.price("claude-haiku-5-5", {})
    assert rates is not None
    at_limit = provider_anthropic.cost(100_000, 1_000_000, rates)
    over = provider_anthropic.cost(100_001, 1_000_000, rates)
    assert at_limit == pytest.approx(100_000 * 0.10 / 1e6 + 1_000_000 * 0.50 / 1e6)
    assert over == pytest.approx(100_001 * 0.50 / 1e6 + 1_000_000 * 2.50 / 1e6)


@pytest.mark.design("prices.shipped_file")
def test_a_search_settles_at_the_pages_fee() -> None:
    rates = provider_anthropic.price("claude-sonnet-5-5", {})
    assert rates is not None
    assert dict(rates.fees)["web_search_request"] == 0.01
    assert provider_anthropic.cost(0, 0, rates, searches=3) == pytest.approx(0.03)


@pytest.mark.design("prices.shipped_file")
def test_check_passes_a_claude_agent_with_no_prices_of_its_own_and_shows_the_date(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    text = next(bad for bad in BAD if bad.name == "openai_refuse_form").text
    claude = text.replace("model:openai", "model:anthropic")
    assert "[agent.prices]" not in claude and "max_cost_usd" not in claude
    project.toml.write_text(claude)
    assert cli.main(["check", str(project.toml)]) == outcomes.OK
    out = capsys.readouterr().out
    assert "a budget of $5" in out and "Anthropic prices as of 2026-11-25" in out
