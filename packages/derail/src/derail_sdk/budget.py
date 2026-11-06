"""The budget for a whole test, kept in one place: the runner.

A call's price is only known once it finishes, so the SDK holds back the most it could
be first. Each hold counts against the limit until the real price replaces it, so calls
running at once cannot each see the same money as free.
"""

import threading


class Ledger:
    def __init__(self, limit_usd: float | None) -> None:
        self.limit_usd = limit_usd  # None: no budget, on purpose
        self.spent = 0.0
        self.reserved = 0.0
        self._held: dict[int, float] = {}
        self._next = 0
        self._lock = threading.Lock()

    def reserve(self, usd: float) -> int | None:
        """A reservation number, or None when the budget cannot cover the call."""
        with self._lock:
            if self.limit_usd is not None and self.spent + self.reserved + usd > self.limit_usd:
                return None
            self._next += 1
            self._held[self._next] = usd
            self.reserved += usd
            return self._next

    def settle(self, reservation: int, usd: float) -> None:
        """Replace a reservation with what the call really cost."""
        with self._lock:
            self.reserved -= self._held.pop(reservation, 0.0)
            self.spent += usd
