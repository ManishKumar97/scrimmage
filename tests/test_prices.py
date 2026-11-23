"""Model prices as data: the shipped file holds every price the code once held."""

import dataclasses
from pathlib import Path

import pytest

import config_cases
from scrimmage import cli, config, plan, prices, provider_openai
from scrimmage.errors import ConfigError
from scrimmage.prices import Rates

ALIASES = provider_openai.TIER_ALIASES

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


# OpenAI's pricing page as read by hand on 22 November 2026, every row the file holds: the rates
# of the standard tier, then each other tier and the long-context rates. The fast tier is written
# once; Rates.at finds it under priority too. The Ultrafast rows of gpt-6.2-astra and gpt-6.2-sol
# are the page's list rates, 6 times Standard, from the "Ultrafast list pricing data" table.
PAGE = {
    "gpt-4o": Rates(2.5, 10.0, 1.25, tiers=(("fast", Rates(4.25, 17.0, 2.125)),)),
    "gpt-4o-2024-05-13": Rates(5.0, 15.0),
    "gpt-4o-mini": Rates(0.15, 0.6, 0.075, tiers=(("fast", Rates(0.25, 1.0, 0.125)),)),
    "gpt-4.1": Rates(2.0, 8.0, 0.5, tiers=(("fast", Rates(3.5, 14.0, 0.875)),)),
    "gpt-4.1-mini": Rates(0.4, 1.6, 0.1, tiers=(("fast", Rates(0.7, 2.8, 0.175)),)),
    "gpt-4.1-nano": Rates(0.1, 0.4, 0.025),
    "gpt-5": Rates(
        1.25,
        10.0,
        0.125,
        tiers=(
            ("flex", Rates(0.625, 5.0, 0.0625)),
            ("fast", Rates(2.5, 20.0, 0.25)),
        ),
    ),
    "gpt-5-mini": Rates(
        0.25,
        2.0,
        0.025,
        tiers=(
            ("flex", Rates(0.125, 1.0, 0.0125)),
            ("fast", Rates(0.45, 3.6, 0.045)),
        ),
    ),
    "gpt-5-nano": Rates(0.05, 0.4, 0.005, tiers=(("flex", Rates(0.025, 0.2, 0.0025)),)),
    "gpt-5-pro": Rates(15.0, 120.0),
    "gpt-5.1": Rates(
        1.25,
        10.0,
        0.125,
        tiers=(
            ("flex", Rates(0.625, 5.0, 0.0625)),
            ("fast", Rates(2.5, 20.0, 0.25)),
        ),
    ),
    "gpt-5.2": Rates(
        1.75,
        14.0,
        0.175,
        tiers=(
            ("flex", Rates(0.875, 7.0, 0.0875)),
            ("fast", Rates(3.5, 28.0, 0.35)),
        ),
    ),
    "gpt-5.2-pro": Rates(21.0, 168.0),
    "gpt-5.3-codex": Rates(1.75, 14.0, 0.175, tiers=(("fast", Rates(3.5, 28.0, 0.35)),)),
    "gpt-5.4-mini": Rates(
        0.75,
        4.5,
        0.075,
        tiers=(
            ("flex", Rates(0.375, 2.25, 0.0375)),
            ("fast", Rates(1.5, 9.0, 0.15)),
        ),
    ),
    "gpt-5.4-nano": Rates(0.2, 1.25, 0.02, tiers=(("flex", Rates(0.1, 0.625, 0.01)),)),
    "gpt-6.2-astra": Rates(
        8.0,
        40.0,
        0.4,
        10.0,
        20.0,
        above=272000,
        long=Rates(16.0, 60.0, 0.8, 20.0, 40.0),
        tiers=(
            (
                "fast",
                Rates(
                    16.0,
                    80.0,
                    0.8,
                    20.0,
                    40.0,
                    above=272000,
                    long=Rates(32.0, 120.0, 1.6, 40.0, 80.0),
                ),
            ),
            (
                "ultrafast",
                Rates(
                    48.0,
                    240.0,
                    2.4,
                    60.0,
                    120.0,
                    above=272000,
                    long=Rates(96.0, 360.0, 4.8, 120.0, 240.0),
                ),
            ),
        ),
    ),
    "gpt-6.2-sol": Rates(
        2.0,
        10.0,
        0.1,
        2.5,
        5.0,
        above=272000,
        long=Rates(4.0, 15.0, 0.2, 5.0, 10.0),
        tiers=(
            (
                "fast",
                Rates(
                    4.0, 20.0, 0.2, 5.0, 10.0, above=272000, long=Rates(8.0, 30.0, 0.4, 10.0, 20.0)
                ),
            ),
            (
                "ultrafast",
                Rates(
                    12.0,
                    60.0,
                    0.6,
                    15.0,
                    30.0,
                    above=272000,
                    long=Rates(24.0, 90.0, 1.2, 30.0, 60.0),
                ),
            ),
        ),
    ),
    "gpt-6-astra": Rates(
        10.0,
        50.0,
        1.0,
        12.5,
        25.0,
        above=272000,
        long=Rates(20.0, 75.0, 2.0, 25.0, 50.0),
        tiers=(
            (
                "flex",
                Rates(
                    5.0,
                    25.0,
                    0.5,
                    6.25,
                    12.5,
                    above=272000,
                    long=Rates(10.0, 37.5, 1.0, 12.5, 25.0),
                ),
            ),
            (
                "fast",
                Rates(
                    20.0,
                    100.0,
                    2.0,
                    25.0,
                    50.0,
                    above=272000,
                    long=Rates(40.0, 150.0, 4.0, 50.0, 100.0),
                ),
            ),
            (
                "ultrafast",
                Rates(
                    60.0,
                    300.0,
                    6.0,
                    75.0,
                    150.0,
                    above=272000,
                    long=Rates(120.0, 450.0, 12.0, 150.0, 300.0),
                ),
            ),
        ),
    ),
    "gpt-6.1-sol": Rates(
        2.0,
        10.0,
        0.1,
        2.5,
        5.0,
        above=272000,
        long=Rates(4.0, 15.0, 0.2, 5.0, 10.0),
        tiers=(
            (
                "flex",
                Rates(1.0, 5.0, 0.05, 1.25, 2.5, above=272000, long=Rates(2.0, 7.5, 0.1, 2.5, 5.0)),
            ),
            (
                "fast",
                Rates(
                    4.0, 20.0, 0.2, 5.0, 10.0, above=272000, long=Rates(8.0, 30.0, 0.4, 10.0, 20.0)
                ),
            ),
            (
                "ultrafast",
                Rates(
                    12.0,
                    60.0,
                    0.6,
                    15.0,
                    30.0,
                    above=272000,
                    long=Rates(24.0, 90.0, 1.2, 30.0, 60.0),
                ),
            ),
        ),
    ),
    "gpt-6.1-terra": Rates(
        1.0,
        5.0,
        0.05,
        1.25,
        2.5,
        above=272000,
        long=Rates(2.0, 7.5, 0.1, 2.5, 5.0),
        tiers=(
            (
                "flex",
                Rates(
                    0.5,
                    2.5,
                    0.025,
                    0.625,
                    1.25,
                    above=272000,
                    long=Rates(1.0, 3.75, 0.05, 1.25, 2.5),
                ),
            ),
            (
                "fast",
                Rates(
                    2.0, 10.0, 0.1, 2.5, 5.0, above=272000, long=Rates(4.0, 15.0, 0.2, 5.0, 10.0)
                ),
            ),
        ),
    ),
    "gpt-6-luna": Rates(
        0.1,
        0.5,
        0.01,
        0.125,
        0.25,
        above=272000,
        long=Rates(0.2, 0.75, 0.02, 0.25, 0.5),
        tiers=(
            (
                "flex",
                Rates(
                    0.05,
                    0.25,
                    0.005,
                    0.0625,
                    0.125,
                    above=272000,
                    long=Rates(0.1, 0.375, 0.01, 0.125, 0.25),
                ),
            ),
            (
                "fast",
                Rates(
                    0.2, 1.0, 0.02, 0.25, 0.5, above=272000, long=Rates(0.4, 1.5, 0.04, 0.5, 1.0)
                ),
            ),
        ),
    ),
    "gpt-6-sol": Rates(
        2.0,
        10.0,
        0.2,
        2.5,
        5.0,
        above=272000,
        long=Rates(4.0, 15.0, 0.4, 5.0, 10.0),
        tiers=(
            (
                "flex",
                Rates(1.0, 5.0, 0.1, 1.25, 2.5, above=272000, long=Rates(2.0, 7.5, 0.2, 2.5, 5.0)),
            ),
            (
                "fast",
                Rates(
                    4.0, 20.0, 0.4, 5.0, 10.0, above=272000, long=Rates(8.0, 30.0, 0.8, 10.0, 20.0)
                ),
            ),
        ),
    ),
    "gpt-5.6-terra": Rates(
        2.0,
        12.0,
        0.2,
        2.5,
        5.0,
        above=272000,
        long=Rates(4.0, 18.0, 0.4, 5.0, 10.0),
        tiers=(
            (
                "flex",
                Rates(1.0, 6.0, 0.1, 1.25, 2.5, above=272000, long=Rates(2.0, 9.0, 0.2, 2.5, 5.0)),
            ),
            (
                "fast",
                Rates(
                    4.0, 24.0, 0.4, 5.0, 10.0, above=272000, long=Rates(8.0, 36.0, 0.8, 10.0, 20.0)
                ),
            ),
        ),
    ),
    "gpt-5.6-luna": Rates(
        0.2,
        1.2,
        0.02,
        0.25,
        0.5,
        above=272000,
        long=Rates(0.4, 1.8, 0.04, 0.5, 1.0),
        tiers=(
            (
                "flex",
                Rates(
                    0.1, 0.6, 0.01, 0.125, 0.25, above=272000, long=Rates(0.2, 0.9, 0.02, 0.25, 0.5)
                ),
            ),
            (
                "fast",
                Rates(0.4, 2.4, 0.04, 0.5, 1.0, above=272000, long=Rates(0.8, 3.6, 0.08, 1.0, 2.0)),
            ),
        ),
    ),
    "gpt-5.5": Rates(
        5.0,
        30.0,
        0.5,
        above=272000,
        long=Rates(10.0, 45.0, 1.0),
        tiers=(
            ("flex", Rates(2.5, 15.0, 0.25, above=272000, long=Rates(5.0, 22.5, 0.5))),
            ("fast", Rates(12.5, 75.0, 1.25)),
        ),
    ),
    "gpt-5.5-pro": Rates(30.0, 180.0, above=272000, long=Rates(60.0, 270.0)),
    "gpt-5.4": Rates(
        2.5,
        15.0,
        0.25,
        above=272000,
        long=Rates(5.0, 22.5, 0.5),
        tiers=(
            ("flex", Rates(1.25, 7.5, 0.13, above=272000, long=Rates(2.5, 11.25, 0.25))),
            ("fast", Rates(5.0, 30.0, 0.5)),
        ),
    ),
    "gpt-5.4-pro": Rates(
        30.0,
        180.0,
        above=272000,
        long=Rates(60.0, 270.0),
        tiers=(("flex", Rates(15.0, 90.0, above=272000, long=Rates(30.0, 135.0))),),
    ),
    "o3-pro": Rates(20.0, 80.0),
    "o3": Rates(
        2.0,
        8.0,
        0.5,
        tiers=(
            ("flex", Rates(1.0, 4.0, 0.25)),
            ("fast", Rates(3.5, 14.0, 0.875)),
        ),
    ),
    "chat-latest": Rates(5.0, 30.0, 0.5),
    "gpt-5-search-api": Rates(1.25, 10.0, 0.125),
    "text-embedding-3-small": Rates(0.02, 0.0),
    "text-embedding-3-large": Rates(0.13, 0.0),
    "text-embedding-ada-002": Rates(0.1, 0.0),
}


@pytest.mark.design("prices.shipped_file")
def test_the_shipped_file_holds_the_rows_read_from_the_page() -> None:
    shipped = prices.shipped()
    section = shipped.section("openai")
    assert section is not None
    assert dict(section.models) == PAGE
    assert section.as_of == "2026-11-22"
    assert section.url == "https://developers.openai.com/api/docs/pricing"


@pytest.mark.design("prices.shipped_file")
def test_the_file_still_holds_every_row_of_the_old_table_at_equal_standard_rates() -> None:
    plain = {
        model: dataclasses.replace(r, tiers=(), above=None, long=None)
        for model, r in dict(prices.shipped().models("openai")).items()
    }
    for model, rates in TODAYS.items():
        assert plain[model] == rates, model


@pytest.mark.design("prices.shipped_file")
def test_a_model_with_a_long_price_is_in_the_file_with_its_threshold() -> None:
    for model in (
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ):
        rates = dict(prices.shipped().models("openai"))[model]
        assert rates.above == 272000 and rates.long is not None, model
    # A short call is at the short rate, a long one at the long rate, the whole call.
    five = dict(prices.shipped().models("openai"))["gpt-5.5"]
    assert five.sized(272000).input == 5.0
    assert five.sized(272001).input == 10.0 and five.sized(272001).output == 45.0


@pytest.mark.design("prices.tier_alias")
def test_fast_and_priority_are_one_tier_and_no_batch_tier_is_priced() -> None:
    for model, rates in prices.shipped().models("openai"):
        names = [name for name, _ in rates.tiers]
        assert "priority" not in names and "batch" not in names, model
        assert rates.at("fast", ALIASES) is rates.at("priority", ALIASES), model
        if "fast" in names:
            assert rates.at("priority", ALIASES) is not rates, model


@pytest.mark.design("prices.tier_alias")
def test_a_row_with_only_one_of_fast_and_priority_prices_both_names() -> None:
    fast, priority = Rates(4.0, 8.0), Rates(5.0, 9.0)
    only_fast = Rates(1.0, 2.0, tiers=(("fast", fast),))
    only_priority = Rates(1.0, 2.0, tiers=(("priority", priority),))
    assert only_fast.at("fast", ALIASES) is fast and only_fast.at("priority", ALIASES) is fast
    assert (
        only_priority.at("priority", ALIASES) is priority
        and only_priority.at("fast", ALIASES) is priority
    )
    both = Rates(1.0, 2.0, tiers=(("fast", fast), ("priority", priority)))
    assert (
        both.at("fast", ALIASES) is fast and both.at("priority", ALIASES) is priority
    )  # its own name first
    assert only_fast.at("flex") is only_fast and only_fast.at(None) is only_fast


@pytest.mark.design("prices.tier_alias")
def test_a_team_row_with_only_tiers_fast_prices_a_reply_naming_priority(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT
        + '\n[agent.prices."gpt-4.1"]\ninput = 2.0\noutput = 8.0\n'
        + '\n[agent.prices."gpt-4.1".tiers.fast]\ninput = 3.5\noutput = 14.0\n'
        + config_cases.TEST
    )
    rates = dict(config.load(tmp_path / "scrimmage.toml").agent.prices)["gpt-4.1"]
    assert rates.at("priority", ALIASES).input == 3.5 and rates.at("fast", ALIASES).input == 3.5


@pytest.mark.design("prices.ultrafast_62")
@pytest.mark.parametrize("model", ["gpt-6.2-astra", "gpt-6.2-sol"])
def test_ultrafast_on_a_62_model_is_at_least_three_times_standard(model: str) -> None:
    rates = dict(prices.shipped().models("openai"))[model]
    ultra = rates.at("ultrafast")
    assert ultra is not rates
    assert ultra.input >= 3 * rates.input and ultra.output >= 3 * rates.output
    assert rates.long is not None and ultra.long is not None
    assert ultra.long.input >= 3 * rates.long.input and ultra.long.output >= 3 * rates.long.output


@pytest.mark.design("prices.shipped_file")
def test_the_code_no_longer_holds_a_price_table() -> None:
    from scrimmage import provider_openai

    assert not hasattr(provider_openai, "PRICES")


TEAM_FILE = """
[openai]
url = "https://example.test/prices"
as_of = "2026-11-20"

[openai.models."gpt-4.1"]
input = 7.0
output = 7.0

[openai.models."gpt-4o"]
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
    held = dict(a_plan(tmp_path).prices_for("openai"))

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
    held = dict(a_plan(tmp_path, team=False).prices_for("openai"))
    found = provider_openai.price("gpt-4o", held)
    assert found == dict(prices.shipped().models("openai"))["gpt-4o"]


@pytest.mark.design("prices.order")
def test_a_bad_team_file_is_a_config_error_naming_the_line(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(config_cases.MINIMAL)
    (tmp_path / "scrimmage-runs").mkdir()
    bad = TEAM_FILE.replace("input = 7.0", 'input = "seven"')
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(bad)
    with pytest.raises(ConfigError) as caught:
        config.load(tmp_path / "scrimmage.toml")
    assert "scrimmage-runs/prices.toml openai models gpt-4.1 input" in caught.value.problems[0]
    assert "(line 6)" in caught.value.problems[0]


@pytest.mark.design("prices.order")
def test_check_names_each_prices_source_and_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config.load(_written(tmp_path))
    assert prices.sources((), None) == []
    lines = prices.sources(cfg.agent.prices, cfg.team_prices)
    assert lines == [
        "price gpt-4.1: [agent.prices]",
        "prices: scrimmage-runs/prices.toml, OpenAI prices as of 2026-11-20 (2 models)",
        "prices: the shipped prices.toml, OpenAI prices as of 2026-11-22 (36 models)",
    ]
    assert cli.main(["check", str(tmp_path / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "a budget of $5, OpenAI prices as of 2026-11-20\n" + "\n".join(lines) in out


def _written(tmp_path: Path) -> Path:
    a_plan(tmp_path)
    return tmp_path / "scrimmage.toml"


HEAD = '[openai]\nurl = "https://example.test/prices"\nas_of = "2026-11-20"\n'
BASE = '[openai.models."gpt-4.1"]\ninput = 2\noutput = 8\n[openai.models."gpt-4.1".tiers.'
FREE_FILES = {
    "input 0": ('[openai.models."gpt-4.1"]\ninput = 0\noutput = 8\n', "input: 0 is below 0.001"),
    "output 0": ('[openai.models."gpt-4.1"]\ninput = 2\noutput = 0\n', "output: 0 is below 0.001"),
    "tier input 0": (
        BASE + "flex]\ninput = 0\noutput = 4\n",
        "tiers flex input: 0 is below 0.001",
    ),
    "tier output 0": (
        BASE + "flex]\ninput = 1\noutput = 0\n",
        "tiers flex output: 0 is below 0.001",
    ),
    "tier default": (
        BASE + "default]\ninput = 1\noutput = 4\n",
        "tiers default: default is not a tier a file may price",
    ),
    "tier auto": (
        BASE + "auto]\ninput = 1\noutput = 4\n",
        "tiers auto: auto is not a tier a file may price",
    ),
    "input 1e-300": (
        '[openai.models."gpt-4.1"]\ninput = 1e-300\noutput = 8\n',
        "input: 1e-300 is below 0.001",
    ),
    "input just under": (
        '[openai.models."gpt-4.1"]\ninput = 0.0009\noutput = 8\n',
        "input: 0.0009 is below 0.001",
    ),
    "output 1e-300": (
        '[openai.models."gpt-4.1"]\ninput = 2\noutput = 1e-300\n',
        "output: 1e-300 is below 0.001",
    ),
    "tier 1e-300": (
        BASE + "priority]\ninput = 1e-300\noutput = 4\n",
        "tiers priority input: 1e-300 is below 0.001",
    ),
    "tier Default": (
        BASE + "Default]\ninput = 1\noutput = 4\n",
        "tiers Default: Default is not a tier",
    ),
    "tier AUTO": (BASE + "AUTO]\ninput = 1\noutput = 4\n", "tiers AUTO: AUTO is not a tier"),
    "cached 0": (
        '[openai.models."gpt-4.1"]\ninput = 2\noutput = 8\ncached_input = 0\n',
        "cached_input: 0 is below 0.001",
    ),
    "cached 1e-300": (
        '[openai.models."gpt-4.1"]\ninput = 2\noutput = 8\ncached_input = 1e-300\n',
        "cached_input: 1e-300 is below 0.001",
    ),
    "tier cached 0": (
        BASE + "flex]\ninput = 1\noutput = 4\ncached_input = 0\n",
        "tiers flex cached_input: 0 is below 0.001",
    ),
    "embedding cached 0": (
        '[openai.models."text-embedding-9"]\ninput = 0.02\noutput = 0\ncached_input = 0\n',
        "cached_input: 0 is below 0.001",
    ),
    "not finite": (
        '[openai.models."gpt-4.1"]\ninput = inf\noutput = 8\n',
        "input: expected dollars",
    ),
    "nan": ('[openai.models."gpt-4.1"]\ninput = 2\noutput = nan\n', "output: expected dollars"),
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
    body = '[openai.models."text-embedding-9"]\ninput = 0.02\noutput = 0\n'
    assert (
        dict(prices.parse(HEAD + body, "the file").models("openai"))["text-embedding-9"].output
        == 0.0
    )


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
    free = HEAD + '[openai.models."gpt-4.1"]\ninput = 0\noutput = 0\n'
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(free)
    with pytest.raises(
        ConfigError, match=r"scrimmage-runs/prices\.toml openai models gpt-4\.1 input: 0"
    ):
        config.load(tmp_path / "scrimmage.toml")


MINE, FILE_EXACT, SHIPPED_ROW = Rates(50.0, 50.0), Rates(1e-3, 1e-3), Rates(7.0, 7.0)


@pytest.mark.design("prices.authority_first")
def test_a_family_row_of_a_higher_table_beats_an_exact_dated_row_of_a_lower_one() -> None:
    dated = "gpt-4.1-2025-04-14"
    assert (
        prices.price(dated, {"gpt-4.1": MINE}, {dated: FILE_EXACT}, family=prices.dashed_family)
        == MINE
    )
    assert (
        prices.price(
            dated, {}, {dated: FILE_EXACT}, {"gpt-4.1": SHIPPED_ROW}, family=prices.dashed_family
        )
        == FILE_EXACT
    )
    assert (
        prices.price("gpt-4.1", {"gpt-4.1": MINE}, {dated: FILE_EXACT}, family=prices.dashed_family)
        == MINE
    )


@pytest.mark.design("prices.authority_first")
def test_within_one_table_the_exact_dated_row_beats_its_family() -> None:
    dated = "gpt-4.1-2025-04-14"
    assert (
        prices.price(dated, {"gpt-4.1": MINE, dated: FILE_EXACT}, family=prices.dashed_family)
        == FILE_EXACT
    )


@pytest.mark.design("prices.authority_first")
def test_a_fetched_dated_row_cannot_undercut_the_teams_own_family_row(tmp_path: Path) -> None:
    dated = "gpt-4.1-2025-04-14"
    (tmp_path / "scrimmage.toml").write_text(
        config_cases.AGENT
        + '\n[agent.prices."gpt-4.1"]\ninput = 50.0\noutput = 50.0\n'
        + config_cases.TEST
    )
    (tmp_path / "scrimmage-runs").mkdir()
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(
        f'[openai]\nurl = "x"\nas_of = "2026-11-20"\n'
        f'[openai.models."{dated}"]\ninput = 0.01\noutput = 0.01\n'
    )
    cfg = config.load(tmp_path / "scrimmage.toml")
    held = dict(plan.plan_for(cfg, plan.rows(cfg)[0], "run-1").prices_for("openai"))
    for name in (dated, "gpt-4.1"):
        found = provider_openai.price(name, held)
        assert found is not None and found.input == 50.0, name
    # The shipped dated row still beats nothing: with no team row, it is its own price.
    assert provider_openai.price("gpt-4o-2024-05-13", {}) == Rates(5.00, 15.00)


@pytest.mark.design("prices.file_never_free")
def test_a_price_at_the_floor_is_accepted_and_an_embedding_model_needs_no_output() -> None:
    body = '[openai.models."gpt-4.1"]\ninput = 0.001\noutput = 0.001\n'
    body += '[openai.models."text-embedding-9"]\ninput = 0.001\noutput = 0\n'
    assert len(prices.parse(HEAD + body, "the file").models("openai")) == 2


@pytest.mark.design("prices.file_never_free")
def test_a_cached_rate_at_the_floor_is_accepted_in_a_file() -> None:
    body = '[openai.models."gpt-4.1"]\ninput = 2\noutput = 8\ncached_input = 0.001\n'
    assert (
        dict(prices.parse(HEAD + body, "the file").models("openai"))["gpt-4.1"].cached_input
        == 0.001
    )


def test_the_lookup_and_the_plans_dedupe_read_one_snapshot_rule_from_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scrimmage.providers.registry import built_in

    provider = built_in().get("openai")
    team = Rates(1.0, 2.0)
    assert provider.snapshot_family("gpt-4.1-2025-04-14") == "gpt-4.1"
    assert provider.price("gpt-4.1-2025-04-14", {"gpt-4.1": team}) == team
    # The rule has one definition. Change it, and the lookup follows, so the lookup and the
    # dedupe in plan.py, which reads provider.snapshot_family, cannot hold two rules.
    monkeypatch.setattr(provider_openai, "snapshot_family", lambda model: "family-of-all")
    assert provider.snapshot_family("gpt-4.1-2025-04-14") == "family-of-all"
    assert provider.price("gpt-4.1-2025-04-14", {"family-of-all": team}) == team
    assert provider.price("gpt-4.1-2025-04-14", {"gpt-4.1": team}) is None


@pytest.mark.design("prices.tier_alias")
def test_each_provider_owns_its_tier_aliases_and_anthropics_are_none() -> None:
    """OpenAI's priority is its fast tier renamed. Anthropic's fast mode and its Priority Tier are
    two things, so a row of Anthropic's prices neither as the other."""
    from scrimmage.providers.registry import built_in

    fast, priority = Rates(10.0, 50.0), Rates(6.0, 30.0)
    only_fast = Rates(5.0, 25.0, tiers=(("fast", fast),))
    only_priority = Rates(5.0, 25.0, tiers=(("priority", priority),))
    anthropic = built_in().get("anthropic")
    openai = built_in().get("openai")
    for row, own, other in ((only_fast, "fast", "priority"), (only_priority, "priority", "fast")):
        mine = anthropic.price("m", {"m": row})
        assert mine is not None and mine.aliases == ()
        assert mine.at(other) is mine, "no alias: the other name is no tier of this row"
        assert mine.unpriced(other), "so a reply naming it is flagged, not priced at the other"
        assert mine.at(own) is not mine
        theirs = openai.price("m", {"m": row})
        assert theirs is not None
        assert theirs.at(other) is not theirs, "OpenAI's two names are one tier"
        assert not theirs.unpriced(other)


@pytest.mark.design("prices.tier_alias")
def test_a_row_is_equal_to_the_row_a_provider_made_of_it() -> None:
    from scrimmage.providers.registry import built_in

    row = Rates(2.0, 8.0, tiers=(("fast", Rates(4.0, 16.0)),))
    for name in ("openai", "anthropic"):
        made = built_in().get(name).price("m", {"m": row})
        assert made == row
