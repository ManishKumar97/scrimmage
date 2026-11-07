"""The budget for a whole test, kept in one place: the runner.

A call's price is only known once it finishes, so the SDK holds back the most it could
be first. Each hold counts against the limit until the real price replaces it, so calls
running at once cannot each see the same money as free.

A call that only other holds keep out may wait for them to settle: a settle can free
room, and money spent never can. A call that spent money alone cannot cover is refused
at once.
"""

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Shortfall:
    """Why a reservation was refused, with the numbers at that moment."""

    why: str  # "spent": money spent leaves no room; "held": calls in flight hold the rest
    usd: float  # what the call could cost
    spent: float
    held: float
    limit: float


class Ledger:
    def __init__(self, limit_usd: float | None) -> None:
        self.limit_usd = limit_usd  # None: no budget, on purpose
        self.spent = 0.0
        self.reserved = 0.0
        self._held: dict[int, float] = {}
        self._next = 0
        self._settled = threading.Condition()

    def reserve(self, usd: float, wait_s: float = 0.0) -> int | Shortfall:
        """A reservation number, or why the budget cannot cover the call.

        When only holds keep the call out, wait up to wait_s for settles to free room.
        """
        deadline = time.monotonic() + wait_s
        with self._settled:
            while True:
                limit = self.limit_usd
                if limit is None or self.spent + self.reserved + usd <= limit:
                    self._next += 1
                    self._held[self._next] = usd
                    self.reserved += usd
                    return self._next
                why = "spent" if self.spent + usd > limit else "held"
                left = deadline - time.monotonic()
                if why == "spent" or left <= 0:
                    return Shortfall(why, usd, self.spent, self.reserved, limit)
                self._settled.wait(left)

    def settle(self, reservation: int, usd: float) -> None:
        """Replace a reservation with what the call really cost, and wake the waiters."""
        with self._settled:
            self.reserved -= self._held.pop(reservation, 0.0)
            if not self._held:
                self.reserved = 0.0  # no rounding left over for a waiter to wait on
            self.spent += usd
            self._settled.notify_all()
