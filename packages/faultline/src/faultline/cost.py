"""What runs cost, from the tokens the proxy saw and prices the team gives.

There is no built-in price table. Prices change, and a stale table would mislead.
The prices come from the config's [cost] section or from flags, in US dollars per
million tokens. Without them, Faultline counts tokens and says the cost is unknown.
"""

from dataclasses import dataclass

from faultline.model import Run

MILLION = 1_000_000


@dataclass(frozen=True)
class Prices:
    input_per_mtok: float  # US dollars per million input tokens
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / MILLION


def tokens(run: Run | None) -> tuple[int, int]:
    """The input and output tokens of a run, as its trace records them."""
    if run is None:
        return 0, 0
    return (
        sum(step.input_tokens or 0 for step in run.steps),
        sum(step.output_tokens or 0 for step in run.steps),
    )


class Budget:
    """A spending cap shared by every run of a test, checked by the model proxy.

    `spent` starts at what earlier runs cost. The proxy adds each reply's tokens.
    """

    def __init__(self, cap: float, prices: Prices, spent: float = 0.0) -> None:
        self.cap = cap
        self.prices = prices
        self.spent = spent

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.spent += self.prices.cost(input_tokens, output_tokens)

    @property
    def reached(self) -> bool:
        return self.spent >= self.cap


def dollars(amount: float) -> str:
    """$0.19, or "under $0.01" for a cost that rounds to nothing but is not zero."""
    if 0 < amount < 0.005:
        return "under $0.01"
    return f"${amount:,.2f}"
