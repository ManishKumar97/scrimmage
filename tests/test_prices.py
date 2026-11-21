"""Model prices as data: the shipped file holds every price the code once held."""

import pytest

from scrimmage import prices
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
