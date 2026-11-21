"""Model prices as data: the shipped file holds every price the code once held."""

from pathlib import Path

import pytest

import config_cases
from scrimmage import cli, config, plan, prices, provider_openai
from scrimmage.errors import ConfigError
from scrimmage.prices import Rates

# A copy of the table that was in provider_openai.py on 19 November 2026, before the prices
# moved to prices.toml. The file must hold every one of these rows with equal rates.
TODAYS = {
    "gpt-4o": Rates(2.50, 10.00, 1.25),
    "gpt-4o-2024-05-13": Rates(5.00, 15.00),
    "gpt-4o-mini": Rates(0.15, 0.60, 0.075),
    "gpt-4.1": Rates(2.00, 8.00, 0.50),
    "gpt-4.1-mini": Rates(0.40, 1.60, 0.10),
    "gpt-4.1-nano": Rates(0.10, 0.40, 0.025),
    "gpt-5": Rates(1.25, 10.00, 0.125),
    "gpt-5-mini": Rates(0.25, 2.00, 0.025),
    "gpt-5-nano": Rates(0.05, 0.40, 0.005),
    "gpt-5-pro": Rates(15.00, 120.00),
    "gpt-5.1": Rates(1.25, 10.00, 0.125),
    "gpt-5.2": Rates(1.75, 14.00, 0.175),
    "gpt-5.2-pro": Rates(21.00, 168.00),
    "gpt-5.3-codex": Rates(1.75, 14.00, 0.175),
    "gpt-5.4-mini": Rates(0.75, 4.50, 0.075),
    "gpt-5.4-nano": Rates(0.20, 1.25, 0.02),
    "text-embedding-3-small": Rates(0.02, 0.0),
    "text-embedding-3-large": Rates(0.13, 0.0),
}


@pytest.mark.design("prices.shipped_file")
def test_the_shipped_file_holds_every_row_of_the_old_table_with_equal_rates() -> None:
    shipped = prices.shipped()
    assert dict(shipped.models) == TODAYS
    assert shipped.as_of == "2026-11-19"
    assert shipped.source.startswith("https://")


@pytest.mark.design("prices.shipped_file")
def test_the_code_no_longer_holds_a_price_table() -> None:
    from scrimmage import provider_openai

    assert not hasattr(provider_openai, "PRICES")


TEAM_FILE = """
as_of = "2026-11-20"
source = "https://example.test/prices"

[models."gpt-4.1"]
input = 7.0
output = 7.0

[models."gpt-4o"]
input = 6.0
output = 6.0
"""
AGENT_PRICES = '\n[agent.prices."gpt-4.1"]\ninput = 9.0\noutput = 9.0\n'


def a_plan(tmp_path: Path, *, team: bool = True) -> plan.Plan:
    (tmp_path / "scrimmage.toml").write_text(config_cases.AGENT + AGENT_PRICES + config_cases.TEST)
    if team:
        (tmp_path / "scrimmage-runs").mkdir()
        (tmp_path / "scrimmage-runs" / "prices.toml").write_text(TEAM_FILE)
    cfg = config.load(tmp_path / "scrimmage.toml")
    return plan.plan_for(cfg, plan.rows(cfg)[0], "run-1")


@pytest.mark.design("prices.order")
def test_agent_prices_then_the_team_file_then_the_shipped_file(tmp_path: Path) -> None:
    held = dict(a_plan(tmp_path).prices)

    def priced(model: str) -> float | None:
        found = provider_openai.price(model, held)
        return None if found is None else found.input

    assert priced("gpt-4.1") == 9.0  # [agent.prices] over the team file's 7 and the shipped 2
    assert priced("gpt-4o") == 6.0  # the team file over the shipped 2.5
    assert priced("gpt-5") == 1.25  # the shipped file
    # A dated name takes its family's price from the same order.
    assert priced("gpt-4.1-2025-04-14") == 9.0
    assert priced("gpt-4o-2024-08-06") == 6.0
    assert priced("gpt-5-2025-08-07") == 1.25
    assert priced("gpt-9") is None


@pytest.mark.design("prices.order")
def test_without_a_team_file_the_shipped_file_prices_the_rest(tmp_path: Path) -> None:
    held = dict(a_plan(tmp_path, team=False).prices)
    found = provider_openai.price("gpt-4o", held)
    assert found == dict(prices.shipped().models)["gpt-4o"]


@pytest.mark.design("prices.order")
def test_a_bad_team_file_is_a_config_error_naming_the_line(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(config_cases.MINIMAL)
    (tmp_path / "scrimmage-runs").mkdir()
    bad = TEAM_FILE.replace("input = 7.0", 'input = "seven"')
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(bad)
    with pytest.raises(ConfigError) as caught:
        config.load(tmp_path / "scrimmage.toml")
    assert "scrimmage-runs/prices.toml models gpt-4.1 input" in caught.value.problems[0]
    assert "(line 5)" in caught.value.problems[0]


@pytest.mark.design("prices.order")
def test_check_names_each_prices_source_and_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config.load(_written(tmp_path))
    assert prices.sources((), None) == []
    lines = prices.sources(cfg.agent.prices, cfg.team_prices)
    assert lines == [
        "price gpt-4.1: [agent.prices]",
        "prices: scrimmage-runs/prices.toml, as of 2026-11-20 (2 models)",
        "prices: the shipped prices.toml, as of 2026-11-19 (18 models)",
    ]
    assert cli.main(["check", str(tmp_path / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "a budget of $5, prices as of 2026-11-20\n" + "\n".join(lines) in out


def _written(tmp_path: Path) -> Path:
    a_plan(tmp_path)
    return tmp_path / "scrimmage.toml"


HEAD = 'as_of = "2026-11-20"\nsource = "https://example.test/prices"\n'
FREE_FILES = {
    "input 0": ('[models."gpt-4.1"]\ninput = 0\noutput = 8\n', "input: 0 is not above 0"),
    "output 0": ('[models."gpt-4.1"]\ninput = 2\noutput = 0\n', "output: 0 is not above 0"),
    "tier input 0": (
        '[models."gpt-4.1"]\ninput = 2\noutput = 8\n[models."gpt-4.1".tiers.flex]\ninput = 0\noutput = 4\n',
        "tiers flex input: 0 is not above 0",
    ),
    "tier output 0": (
        '[models."gpt-4.1"]\ninput = 2\noutput = 8\n[models."gpt-4.1".tiers.flex]\ninput = 1\noutput = 0\n',
        "tiers flex output: 0 is not above 0",
    ),
    "tier default": (
        '[models."gpt-4.1"]\ninput = 2\noutput = 8\n[models."gpt-4.1".tiers.default]\ninput = 1\noutput = 4\n',
        "tiers default: default is not a tier a file may price",
    ),
    "tier auto": (
        '[models."gpt-4.1"]\ninput = 2\noutput = 8\n[models."gpt-4.1".tiers.auto]\ninput = 1\noutput = 4\n',
        "tiers auto: auto is not a tier a file may price",
    ),
    "not finite": ('[models."gpt-4.1"]\ninput = inf\noutput = 8\n', "input: expected dollars"),
    "nan": ('[models."gpt-4.1"]\ninput = 2\noutput = nan\n', "output: expected dollars"),
}


@pytest.mark.design("prices.file_never_free")
@pytest.mark.parametrize("name", FREE_FILES)
def test_a_price_file_may_not_make_a_model_free(name: str) -> None:
    body, message = FREE_FILES[name]
    with pytest.raises(ConfigError, match=message) as caught:
        prices.parse(HEAD + body, "the file")
    assert "(line " in caught.value.problems[0] or "expected dollars" in message


@pytest.mark.design("prices.file_never_free")
def test_an_embedding_model_may_have_no_output_price_in_a_file() -> None:
    body = '[models."text-embedding-9"]\ninput = 0.02\noutput = 0\n'
    assert dict(prices.parse(HEAD + body, "the file").models)["text-embedding-9"].output == 0.0


@pytest.mark.design("prices.file_never_free")
def test_agent_prices_may_still_price_a_model_at_zero(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT + '\n[agent.prices."mine"]\ninput = 0\noutput = 0\n' + config_cases.TEST
    )
    assert dict(config.load(tmp_path / "scrimmage.toml").agent.prices)["mine"].input == 0.0


@pytest.mark.design("prices.file_never_free")
def test_a_team_file_that_makes_a_model_free_stops_the_run_at_load(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(config_cases.MINIMAL)
    (tmp_path / "scrimmage-runs").mkdir()
    free = HEAD + '[models."gpt-4.1"]\ninput = 0\noutput = 0\n'
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(free)
    with pytest.raises(ConfigError, match="scrimmage-runs/prices.toml models gpt-4.1 input: 0"):
        config.load(tmp_path / "scrimmage.toml")
