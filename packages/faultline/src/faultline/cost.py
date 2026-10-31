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
        self.blind = False  # a reply reported no usage, so the cap cannot be kept

    def add(self, used: tuple[int, int] | None) -> None:
        """Add one reply's tokens. None is a reply that reported no usage."""
        if used is None:
            self.blind = True
        else:
            self.spent += self.prices.cost(*used)

    @property
    def reached(self) -> bool:
        """True once no more model calls may go: the cap is spent, or it cannot be
        kept. Fails closed: an unknown cost counts as over the cap."""
        return self.blind or self.spent >= self.cap


NO_USAGE = "cannot enforce --max-cost: the replies report no token usage"


# The error type the proxy gives a call it refused because a reply had no usage.
COST_UNKNOWN = "cost_unknown"


def unmetered(run: Run | None) -> bool:
    """True when the run's cost could not be counted: a model call succeeded with
    no token usage recorded, or the proxy refused a call for that reason."""
    return run is not None and any(
        step.error_type == COST_UNKNOWN
        or (
            step.error_type is None
            and step.input_tokens is None
            and step.output_tokens is None
        )
        for step in run.steps
    )


def dollars(amount: float) -> str:
    """$0.19, or "under $0.01" for a cost that rounds to nothing but is not zero."""
    if 0 < amount < 0.005:
        return "under $0.01"
    return f"${amount:,.2f}"
