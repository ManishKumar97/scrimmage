"""The budget for a whole test, kept in one place: the runner.

A call's price is only known once it finishes, so the SDK holds back the most it could
be first. Each hold counts against the limit until the real price replaces it, so calls
running at once cannot each see the same money as free.

A call that only other holds keep out gets a ticket and asks again until a settle frees
room. The ledger never blocks a request, so a waiter holds no connection, and one that
stops asking is never granted anything. Tickets are served in order, so a large call is
not starved by a stream of small ones. A call that spent money alone cannot cover is
refused at once: spent money never comes back.

A ticket keeps its place until its caller releases it, is granted, or its run ends, however
slowly its caller asks: a loaded machine stretches the time between asks, and a place that
depended on it would be lost by every waiter at once. Only a ticket not asked for in LOST_S
is dropped, for a release that never arrived from a run that is still going.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

# A live waiter asks again within ASK_MAX_S, and an ask that the control server does not
# answer within the client's 10 s socket timeout, twice with a clock retry, ends its wait
# with a refusal. So a ticket not asked for in three times that lost its release.
LOST_S = 60.0
# How soon a held call asks again: the head of the line within ASK_FIRST_S, each ticket
# ahead of it a little later, as it cannot be served before those are. With the gate's
# jitter, 100 waiters ask about 100 times a second, not 533, so a settle is not stuck
# behind a flood of asks.
ASK_FIRST_S = 0.25
ASK_STEP_S = 0.05
ASK_MAX_S = 2.0  # far under LOST_S, so a waiter that follows the hint keeps its place


def ask_again_s(ahead: int) -> float:
    """How long a held call with this many tickets ahead of it waits before asking again."""
    return min(ASK_MAX_S, ASK_FIRST_S + ASK_STEP_S * ahead)


@dataclass(frozen=True)
class Held:
    """One hold, by the run and the call that has it."""

    run: str
    label: str
    usd: float
    ended: bool = False  # its run ended while still holding, so it can never settle


@dataclass(frozen=True)
class Shortfall:
    """Why a reservation was refused, with the numbers at that moment."""

    # "spent": money spent leaves no room. "left": spent money and holds whose run ended
    # leave none, and those never settle. "held": calls in flight hold the rest.
    why: str
    usd: float  # what the call could cost
    spent: float
    held: float
    limit: float
    ticket: int | None = None  # the caller's place in line, to ask again with
    holds: tuple[Held, ...] = ()  # the holds in the way, largest first
    retry_s: float | None = None  # held: how long to wait before asking again


@dataclass
class _Ticket:
    usd: float
    run: str
    asked: float  # when it last asked


class Ledger:
    def __init__(
        self, limit_usd: float | None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit_usd = limit_usd  # None: no budget, on purpose
        self.spent = 0.0
        self.reserved = 0.0
        self._held: dict[int, Held] = {}
        self._line: dict[int, _Ticket] = {}  # waiting calls, in the order they came
        self._closed: set[str] = set()
        self._next = 0
        self._clock = clock
        self._lock = threading.Lock()

    def reserve(
        self, usd: float, run: str = "", label: str = "", ticket: int | None = None
    ) -> int | Shortfall:
        """A reservation number, or why the budget cannot cover the call now.

        Tickets are served first come first served: a call is granted when it fits along with
        every ticket ahead of it, so many calls that all fit are granted at once, and a large
        one at the head is not passed by small ones.
        """
        with self._lock:
            now = self._clock()
            for number in [n for n, t in self._line.items() if now - t.asked > LOST_S]:
                del self._line[number]
            limit = self.limit_usd
            if limit is None:
                return self._grant(usd, run, label, ticket)
            if self.spent + usd > limit:
                self._line.pop(ticket or 0, None)
                return self._short("spent", usd, limit)
            left = sum(h.usd for h in self._held.values() if h.run in self._closed)
            if self.spent + left + usd > limit:
                self._line.pop(ticket or 0, None)
                return self._short("left", usd, limit)
            ahead, place = 0.0, 0
            for number, waiting in self._line.items():
                if number == ticket:
                    break
                ahead += waiting.usd
                place += 1
            if self.spent + self.reserved + ahead + usd <= limit:
                return self._grant(usd, run, label, ticket)
            if ticket is None or ticket not in self._line:
                self._next += 1
                ticket = self._next
            self._line[ticket] = _Ticket(usd, run, now)
            held = self._short("held", usd, limit, ticket)
            return replace(held, retry_s=ask_again_s(place))

    def release(self, ticket: int) -> None:
        """The caller stopped waiting."""
        with self._lock:
            self._line.pop(ticket, None)

    def settle(self, reservation: int, usd: float) -> None:
        """Replace a reservation with what the call really cost. One that is no longer held is
        already settled, and counts nothing again: a settle can arrive twice, such as one
        resent after its reply was lost."""
        with self._lock:
            hold = self._held.pop(reservation, None)
            if hold is None:
                return
            self.reserved -= hold.usd
            if not self._held:
                self.reserved = 0.0  # no rounding left over for a waiter to wait on
            self.spent += usd

    def holder(self, reservation: int) -> str | None:
        """The run that holds this reservation, or None if it is not held."""
        with self._lock:
            hold = self._held.get(reservation)
            return None if hold is None else hold.run

    def close_run(self, run: str) -> None:
        """The run's program has ended. Its tickets go; its holds stay, as a call that was
        sent may have spent the money, and a late settle still replaces them."""
        with self._lock:
            self._closed.add(run)
            for number in [n for n, t in self._line.items() if t.run == run]:
                del self._line[number]

    def leaked(self) -> list[Held]:
        """Holds whose run ended without settling them."""
        with self._lock:
            return [self._seen(h) for h in self._held.values() if h.run in self._closed]

    def _grant(self, usd: float, run: str, label: str, ticket: int | None) -> int:
        self._line.pop(ticket or 0, None)
        self._next += 1
        self._held[self._next] = Held(run, label, usd)
        self.reserved += usd
        return self._next

    def _short(self, why: str, usd: float, limit: float, ticket: int | None = None) -> Shortfall:
        holds = tuple(sorted((self._seen(h) for h in self._held.values()), key=lambda h: -h.usd))
        return Shortfall(why, usd, self.spent, self.reserved, limit, ticket, holds)

    def _seen(self, hold: Held) -> Held:
        return Held(hold.run, hold.label, hold.usd, hold.run in self._closed)
