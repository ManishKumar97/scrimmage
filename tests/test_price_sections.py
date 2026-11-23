"""A price file has one section for each provider, and the keys a second provider needs."""

from pathlib import Path

import pytest

import config_cases
from scrimmage import cli, config, plan, prices, prices_update, provider_openai
from scrimmage.errors import ConfigError
from scrimmage.prices import PriceFile, Rates

TWO = """
[openai]
url = "https://example.test/openai"
as_of = "2026-11-20"

[openai.models."shared"]
input = 2
output = 8

[openai.models."only-openai"]
input = 1
output = 4

[anthropic]
url = "https://example.test/anthropic"
as_of = "2026-11-25"

[anthropic.models."shared"]
input = 3
output = 15
cache_write_5m = 3.75
cache_write_1h = 6

[anthropic.models."claude-haiku-4-5"]
input = 1
output = 5
"""


def two() -> PriceFile:
    return prices.parse(TWO, "the file")


def input_rate(found: Rates | None) -> float | None:
    return None if found is None else found.input


@pytest.mark.design("anthropic.price_sections")
def test_a_lookup_goes_to_the_section_of_the_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prices, "shipped", two)
    assert input_rate(prices.priced("openai", "shared")) == 2
    assert input_rate(prices.priced("anthropic", "shared")) == 3


@pytest.mark.design("anthropic.price_sections")
def test_a_lookup_in_the_wrong_providers_section_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prices, "shipped", two)
    assert prices.priced("anthropic", "only-openai") is None
    assert prices.priced("openai", "claude-haiku-4-5") is None
    assert prices.priced("openai", "claude-haiku-4-5-20251001") is None
    assert input_rate(prices.priced("anthropic", "claude-haiku-4-5-20251001")) == 1
    # A provider with no section prices nothing.
    assert prices.priced("mistral", "shared") is None


@pytest.mark.design("anthropic.price_sections")
def test_the_shipped_openai_table_is_not_looked_up_for_another_provider() -> None:
    assert provider_openai.price("gpt-4.1", {}) is not None
    assert prices.priced("anthropic", "gpt-4.1") is None


TEAM = """
[openai]
url = "x"
as_of = "2026-11-21"

[openai.models."gpt-4.1"]
input = 7
output = 7

[anthropic]
url = "x"
as_of = "2026-11-21"

[anthropic.models."gpt-4o"]
input = 6
output = 6
"""


def a_plan(tmp_path: Path) -> plan.Plan:
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT + '\n[agent.prices."gpt-5"]\ninput = 9\noutput = 9\n' + config_cases.TEST
    )
    (tmp_path / "scrimmage-runs").mkdir()
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(TEAM)
    cfg = config.load(tmp_path / "scrimmage.toml")
    return plan.plan_for(cfg, plan.rows(cfg)[0], "run-1")


@pytest.mark.design("anthropic.price_sections")
def test_a_team_agent_prices_row_wins_over_a_section_row_and_a_file_row(tmp_path: Path) -> None:
    held = dict(a_plan(tmp_path).prices_for("openai"))
    assert input_rate(provider_openai.price("gpt-5", held)) == 9  # not the shipped 1.25
    assert input_rate(provider_openai.price("gpt-5-2025-08-07", held)) == 9  # its family too
    assert input_rate(provider_openai.price("gpt-4.1", held)) == 7  # the team file's openai row
    assert input_rate(provider_openai.price("gpt-4o", held)) == 2.5  # not the anthropic section's 6


@pytest.mark.design("anthropic.price_sections")
def test_the_team_files_section_for_another_provider_prices_no_openai_call(
    tmp_path: Path,
) -> None:
    built = a_plan(tmp_path)
    assert dict(built.prices_for("openai")).keys() == {"gpt-5", "gpt-4.1"}
    assert dict(built.prices_for("anthropic")).keys() == {"gpt-5", "gpt-4o"}


@pytest.mark.design("anthropic.price_sections")
def test_the_team_files_sections_survive_the_plans_json_round_trip(tmp_path: Path) -> None:
    built = a_plan(tmp_path)
    again = plan.Plan.from_json(built.to_json())
    assert again.prices_for("openai") == built.prices_for("openai")
    assert again.prices_for("anthropic") == built.prices_for("anthropic")


FILE = '[anthropic]\nurl = "x"\nas_of = "2026-11-20"\n'


def one(rows: str) -> PriceFile:
    return prices.parse(FILE + rows, "the file")


def only(file: PriceFile, model: str) -> Rates:
    return dict(file.models("anthropic"))[model]


@pytest.mark.design("anthropic.cache_write_keys")
def test_the_new_write_keys_validate_and_price_a_write() -> None:
    row = (
        '[anthropic.models."m"]\ninput = 1\noutput = 5\ncache_write_5m = 1.25\ncache_write_1h = 2\n'
    )
    rates = only(one(row), "m")
    assert (rates.cache_write_5m, rates.cache_write_1h) == (1.25, 2.0)
    assert prices.effective(rates, "cache_write_5m") == 1.25
    assert prices.effective(rates, "cache_write_1h") == 2.0
    # A write rate a row leaves out costs its input rate.
    bare = only(one('[anthropic.models."m"]\ninput = 1\noutput = 5\ncache_write_5m = 1.25\n'), "m")
    assert prices.effective(bare, "cache_write_1h") == 1.0
    # A tier and a long table carry them too, and a long call is priced at the long rates.
    deep = (
        row
        + '[anthropic.models."m".tiers.fast]\ninput = 2\noutput = 10\ncache_write_1h = 4\n'
        + '[anthropic.models."m".long]\nabove = 100000\ninput = 2\noutput = 8\n'
        + "cache_write_5m = 2.5\ncache_write_1h = 4\n"
    )
    found = only(one(deep), "m")
    assert found.at("fast").cache_write_1h == 4.0
    assert found.sized(100001).cache_write_1h == 4.0
    assert found.sized(100000).cache_write_1h == 2.0


WRITE_FILES = {
    "5m below input": (
        "input = 2\noutput = 5\ncache_write_5m = 1.9\n",
        "cache_write_5m: 1.9 is below input, 2",
    ),
    "1h below input": (
        "input = 2\noutput = 5\ncache_write_1h = 1\n",
        "cache_write_1h: 1 is below input, 2",
    ),
    "5m under the floor": (
        "input = 0.0005\noutput = 5\ncache_write_5m = 0.0005\n",
        "input: 0.0005 is below 0.001",
    ),
    "5m 1e-300": (
        "input = 1\noutput = 5\ncache_write_5m = 1e-300\n",
        "cache_write_5m: 1e-300 is below input, 1",
    ),
    "5m not a number": (
        'input = 1\noutput = 5\ncache_write_5m = "a lot"\n',
        "cache_write_5m: expected dollars",
    ),
    "1h negative": (
        "input = 1\noutput = 5\ncache_write_1h = -1\n",
        "cache_write_1h: expected dollars",
    ),
}


@pytest.mark.design("anthropic.cache_write_keys")
@pytest.mark.parametrize("name", WRITE_FILES)
def test_the_new_write_keys_have_the_same_checks_as_the_old_ones(name: str) -> None:
    body, message = WRITE_FILES[name]
    with pytest.raises(ConfigError, match=message):
        one('[anthropic.models."m"]\n' + body)


@pytest.mark.design("anthropic.cache_write_keys")
def test_a_write_rate_at_the_floor_is_accepted_in_a_file() -> None:
    row = '[anthropic.models."m"]\ninput = 0.001\noutput = 0.001\ncache_write_5m = 0.001\n'
    assert only(one(row + "cache_write_1h = 0.001\n"), "m").cache_write_1h == 0.001


@pytest.mark.design("anthropic.cache_write_keys")
def test_a_long_table_may_not_write_for_less_than_its_base() -> None:
    base = '[anthropic.models."m"]\ninput = 1\noutput = 5\ncache_write_1h = 2\n'
    low = '[anthropic.models."m".long]\nabove = 9\ninput = 1\noutput = 5\ncache_write_1h = 1.5\n'
    with pytest.raises(ConfigError, match=r"cache_write_1h: 1\.5 is below the 2 of a short call"):
        one(base + low)
    # A long table that leaves it out costs its input, which here is under the base's 2.
    bare = '[anthropic.models."m".long]\nabove = 9\ninput = 1.5\noutput = 5\n'
    with pytest.raises(ConfigError, match=r"cache_write_1h: 1\.5 is below the 2 of a short call"):
        one(base + bare)


@pytest.mark.design("anthropic.cache_write_keys")
def test_a_team_may_set_the_new_keys_in_agent_prices(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT
        + '\n[agent.prices."claude-x"]\ninput = 1\noutput = 5\ncache_write_5m = 1.25\n'
        + "cache_write_1h = 2\n"
        + config_cases.TEST
    )
    rates = dict(config.load(tmp_path / "scrimmage.toml").agent.prices)["claude-x"]
    assert (rates.cache_write_5m, rates.cache_write_1h) == (1.25, 2.0)
    again = dict(
        plan.Plan.from_json(
            plan.plan_for(
                config.load(tmp_path / "scrimmage.toml"),
                plan.rows(config.load(tmp_path / "scrimmage.toml"))[0],
                "run-1",
            ).to_json()
        ).prices
    )["claude-x"]
    assert again == rates
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT
        + '\n[agent.prices."claude-x"]\ninput = 2\noutput = 5\ncache_write_1h = 1\n'
        + config_cases.TEST
    )
    with pytest.raises(ConfigError, match="cache_write_1h: 1 is below input, 2"):
        config.load(tmp_path / "scrimmage.toml")


@pytest.mark.design("anthropic.dated_names")
@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("gpt-4o", "gpt-4o"),
        # Eight digits that are no date are part of the name.
        ("model-12345678", "model-12345678"),
        ("claude-haiku-4-5-20251301", "claude-haiku-4-5-20251301"),
        ("claude-haiku-4-5-20250230", "claude-haiku-4-5-20250230"),
        ("claude-haiku-4-5-00000000", "claude-haiku-4-5-00000000"),
        # Not eight digits: not a date either.
        ("claude-haiku-4-5-2025100", "claude-haiku-4-5-2025100"),
        ("claude-haiku-4-5-202510011", "claude-haiku-4-5-202510011"),
    ],
)
def test_a_dated_name_falls_back_to_its_family(name: str, family: str) -> None:
    assert prices.family(name) == family


@pytest.mark.design("anthropic.dated_names")
def test_a_dated_anthropic_name_is_priced_as_its_family_and_a_non_date_is_not() -> None:
    table = {"claude-haiku-4-5": Rates(1.0, 5.0)}
    assert prices.price("claude-haiku-4-5-20251001", table) == Rates(1.0, 5.0)
    assert prices.price("claude-haiku-4-5", table) == Rates(1.0, 5.0)
    assert prices.price("claude-haiku-4-5-12345678", table) is None
    assert prices.price("claude-haiku-4-5-20251301", table) is None
    # The exact dated row beats the family's, in one table, as an OpenAI one does.
    exact = {"claude-haiku-4-5": Rates(1.0, 5.0), "claude-haiku-4-5-20251001": Rates(2.0, 6.0)}
    assert prices.price("claude-haiku-4-5-20251001", exact) == Rates(2.0, 6.0)
    # A team's family row beats a file's dated row, as for OpenAI's names.
    mine = {"claude-haiku-4-5": Rates(9.0, 9.0)}
    assert prices.price("claude-haiku-4-5-20251001", mine, exact) == Rates(9.0, 9.0)


@pytest.mark.design("anthropic.dated_names")
def test_a_team_files_dated_row_is_left_out_when_the_team_prices_the_family() -> None:
    agent = (("claude-haiku-4-5", Rates(9.0, 9.0)),)
    file = (("claude-haiku-4-5-20251001", Rates(0.5, 0.5)), ("other", Rates(1.0, 1.0)))
    assert [m for m, _ in prices.in_order(agent, file)] == ["claude-haiku-4-5", "other"]


@pytest.mark.design("anthropic.price_sections")
def test_prices_update_diffs_each_section_on_its_own() -> None:
    new = TWO.replace('as_of = "2026-11-25"', 'as_of = "2026-12-01"').replace(
        "input = 3\noutput = 15", "input = 3.5\noutput = 15"
    )
    lines = prices_update.diff_files(two(), prices.parse(new, "new"))
    assert lines == [
        "openai: as of 2026-11-20, the new file is as of 2026-11-20",
        "  no price changed",
        "anthropic: as of 2026-11-25, the new file is as of 2026-12-01",
        "  changed shared: input 3 to 3.5",
    ]


@pytest.mark.design("anthropic.price_sections")
def test_prices_update_lists_a_section_only_one_file_has_as_added_or_removed() -> None:
    openai_only = TWO[: TWO.index("[anthropic]")]
    grown = prices_update.diff_files(prices.parse(openai_only, "old"), two())
    assert grown[2:] == [
        "anthropic: section added, as of 2026-11-25",
        "  added shared: input 3, output 15, cache_write_5m 3.75, cache_write_1h 6",
        "  added claude-haiku-4-5: input 1, output 5",
    ]
    shrunk = prices_update.diff_files(two(), prices.parse(openai_only, "new"))
    assert shrunk[-1] == "anthropic: section removed"


@pytest.mark.design("anthropic.price_sections")
def test_check_prints_each_providers_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(prices, "shipped", two)
    (tmp_path / "scrimmage.toml").write_text(config_cases.MINIMAL)
    assert cli.main(["check", str(tmp_path / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "OpenAI prices as of 2026-11-20, Anthropic prices as of 2026-11-25" in out
    # A section of the team's file stands for the shipped one, per provider.
    (tmp_path / "scrimmage-runs").mkdir()
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(TEAM)
    assert cli.main(["check", str(tmp_path / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "OpenAI prices as of 2026-11-21, Anthropic prices as of 2026-11-21" in out
    assert "scrimmage-runs/prices.toml, OpenAI prices as of 2026-11-21 (1 models)" in out
    assert "the shipped prices.toml, Anthropic prices as of 2026-11-25 (2 models)" in out


@pytest.mark.design("anthropic.price_sections")
def test_check_json_lists_each_section_of_each_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prices, "shipped", two)
    assert prices.source_list((), None) == [
        {
            "source": "the shipped prices.toml",
            "provider": "openai",
            "as_of": "2026-11-20",
            "models": 2,
        },
        {
            "source": "the shipped prices.toml",
            "provider": "anthropic",
            "as_of": "2026-11-25",
            "models": 2,
        },
    ]


FILE_ERRORS = {
    "the old flat shape": (
        'as_of = "2026-11-20"\nsource = "x"\n[models."m"]\ninput = 1\noutput = 1\n',
        "as_of: expected a provider's section",
    ),
    "a section with no url": (
        '[openai]\nas_of = "2026-11-20"\n[openai.models."m"]\ninput = 1\noutput = 1\n',
        "openai url: expected text",
    ),
    "a section with no date": (
        '[openai]\nurl = "x"\n[openai.models."m"]\ninput = 1\noutput = 1\n',
        "openai as_of: expected a date",
    ),
    "a section with no models": (
        '[openai]\nurl = "x"\nas_of = "2026-11-20"\n',
        "needs at least one model",
    ),
    "an unknown key in a section": (
        '[openai]\nurl = "x"\nas_of = "2026-11-20"\nsource = "x"\n',
        "openai source: unknown setting",
    ),
    "an empty file": ("", "needs at least one provider's section"),
    "a bad section name": (
        '[OpenAI]\nurl = "x"\nas_of = "2026-11-20"\n',
        "OpenAI: expected a provider's section",
    ),
}


@pytest.mark.design("anthropic.price_sections")
@pytest.mark.parametrize("name", FILE_ERRORS)
def test_a_file_that_is_not_in_sections_is_a_config_error(name: str) -> None:
    text, message = FILE_ERRORS[name]
    with pytest.raises(ConfigError, match=message):
        prices.parse(text, "the file")


@pytest.mark.design("anthropic.price_sections")
def test_an_error_names_the_line_of_the_models_table_in_its_own_section() -> None:
    text = TWO.replace(
        '[anthropic.models."shared"]\ninput = 3', '[anthropic.models."shared"]\ninput = 0'
    )
    with pytest.raises(ConfigError) as caught:
        prices.parse(text, "the file")
    assert "the file anthropic models shared input: 0 is below 0.001" in caught.value.problems[0]
    line = text.splitlines().index('[anthropic.models."shared"]') + 1
    assert f"(line {line})" in caught.value.problems[0]
