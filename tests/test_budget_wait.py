"""A call that only holds keep out waits its turn; spent money alone refuses at once.

The ledger never blocks a request: a waiter has a ticket and asks again every step.
"""

import asyncio
import dataclasses
import heapq
import json
import random
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import EXAMPLE_FAULT, Project, Result, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage_core import budget as budget_module
from scrimmage_core import control, http_gate, state
from scrimmage_core.budget import LOST_S, Held, Ledger, Shortfall
from scrimmage_core.client import ControlClient, ControlError
from scrimmage_core.config import Agent
from scrimmage_core.control import ControlServer
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.records import RecordStore
from test_http_gate import BODY, URL, Budget, client, gated, install, make_plan

Fake = Callable[..., FakeOpenAI]
TOKEN = "f" * 64
LABEL = "model:openai"
__all__ = ["gated"]  # the fixture, used by name


class Clock:
    """The gate's clock: time passes only when the gate pauses, so a wait is exact under
    any load. An action set for a time runs once the wait has reached it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        actions: list[tuple[float, Callable[[], object]]] | None = None,
    ) -> None:
        current = state.current()
        assert current is not None, "install the run first"
        self.start = self.now = current.began
        self.slept: list[float] = []
        self.actions = sorted(actions or [], key=lambda action: action[0])
        monkeypatch.setattr(http_gate, "time", self)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 1e-6)  # as a real clock does, it always moves on
        while self.actions and self.waited >= self.actions[0][0] - 1e-9:
            self.actions.pop(0)[1]()

    @property
    def waited(self) -> float:
        return self.now - self.start


def until(event: Callable[[], bool], ceiling: float = 60.0) -> None:
    """Wait for an event; the ceiling only ends a test whose event never comes."""
    end = time.monotonic() + ceiling
    while not event():
        assert time.monotonic() < end, "the event never came"
        time.sleep(0.01)


def granted(answer: int | Shortfall) -> int:
    assert isinstance(answer, int), answer
    return answer


def short(answer: int | Shortfall) -> Shortfall:
    assert isinstance(answer, Shortfall), answer
    return answer


# The ledger


@pytest.mark.design("wait.ledger_tickets")
def test_a_held_call_gets_a_ticket_and_is_granted_when_it_asks_after_a_settle() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06, "run-a", LABEL))
    refused = short(ledger.reserve(0.06, "run-b", LABEL))
    assert refused.why == "held" and refused.ticket is not None
    assert refused.holds == (Held("run-a", LABEL, 0.06),)
    ledger.settle(held, 0.01)
    granted(ledger.reserve(0.06, "run-b", LABEL, refused.ticket))
    assert ledger.spent == pytest.approx(0.01) and ledger.reserved == pytest.approx(0.06)


@pytest.mark.design("wait.spent_refuses_at_once")
def test_money_spent_that_leaves_no_room_refuses_at_once() -> None:
    ledger = Ledger(0.10)
    ledger.settle(granted(ledger.reserve(0.08)), 0.08)
    refused = short(ledger.reserve(0.03))
    assert (refused.why, refused.ticket, refused.spent) == ("spent", None, pytest.approx(0.08))


@pytest.mark.design("wait.no_drift")
def test_holds_that_all_settle_leave_nothing_held() -> None:
    ledger = Ledger(1.0)
    for number in [granted(ledger.reserve(usd)) for usd in (0.1, 0.2, 0.3)]:
        ledger.settle(number, 0.0)
    assert ledger.reserved == 0.0  # exactly, so nothing waits on a rounding error


@pytest.mark.design("wait.fifo")
def test_tickets_are_served_in_order_so_a_large_call_is_not_starved() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06, "run-a"))
    big = short(ledger.reserve(0.05, "run-b")).ticket
    small = short(ledger.reserve(0.03, "run-c"))  # it fits alone, but the big call came first
    assert small.why == "held"
    ledger.settle(held, 0.0)
    granted(ledger.reserve(0.03, "run-c", ticket=small.ticket))  # now both fit
    granted(ledger.reserve(0.05, "run-b", ticket=big))


@pytest.mark.design("wait.no_starvation")
def test_a_large_waiter_gets_its_turn_among_steady_small_calls() -> None:
    """The review's probe, in fixed turns, so no scheduler decides it: four callers keep
    taking $0.03 and giving it back, and one call needs $0.05. Each turn every small
    caller settles what it holds and asks again, then the large call asks."""
    ledger = Ledger(0.10)
    small: list[int | Shortfall | None] = [None] * 4
    big: int | Shortfall | None = None
    for _ in range(20):
        for n in range(4):
            mine = small[n]
            if isinstance(mine, int):
                ledger.settle(mine, 0.00001)
            ticket = mine.ticket if isinstance(mine, Shortfall) else None
            small[n] = ledger.reserve(0.03, "run-small", ticket=ticket)
        ticket = big.ticket if isinstance(big, Shortfall) else None
        big = ledger.reserve(0.05, "run-big", ticket=ticket)
        if isinstance(big, int):
            return
    pytest.fail("the large call never got in: the small calls took every room it freed")


@pytest.mark.design("wait.lost_release")
def test_a_ticket_whose_release_was_lost_is_dropped_after_a_minute() -> None:
    now = [0.0]
    ledger = Ledger(0.10, clock=lambda: now[0])
    held = granted(ledger.reserve(0.06))
    short(ledger.reserve(0.06, "run-gone"))  # its release is lost, and it never asks again
    ledger.settle(held, 0.0)
    now[0] = LOST_S - 1
    assert short(ledger.reserve(0.05, "run-c")).why == "held"  # still behind the lost one
    now[0] = LOST_S + 0.1
    granted(ledger.reserve(0.05, "run-c"))


@pytest.mark.design("wait.slow_asker_keeps_its_place")
def test_a_waiter_that_asks_slowly_keeps_its_place() -> None:
    """A loaded machine stretches the time between asks. The first build dropped a ticket
    not asked for in 2 s, so a slow first waiter went to the back, behind a later one."""
    now = [0.0]
    ledger = Ledger(0.10, clock=lambda: now[0])
    held = granted(ledger.reserve(0.10, "run-0"))
    first = short(ledger.reserve(0.06, "run-a")).ticket
    later = short(ledger.reserve(0.06, "run-b"))
    for second in range(1, 30):  # the later one asks every second, the first not at all
        now[0] = second
        later = short(ledger.reserve(0.06, "run-b", ticket=later.ticket))
    ledger.settle(held, 0.0)
    assert short(ledger.reserve(0.06, "run-b", ticket=later.ticket)).why == "held"
    granted(ledger.reserve(0.06, "run-a", ticket=first))  # 30 s on, still at the head


@pytest.mark.design("wait.line_under_load")
@pytest.mark.parametrize("seed", range(5))
def test_a_hundred_slow_askers_are_served_in_order_once_the_room_frees(seed: int) -> None:
    """The F2 probe, on a fake clock: 100 calls of $0.50 wait behind $1.00 of holds, and
    each asks again 2.1 to 4 s after its last ask, as on a loaded machine. A granted call
    settles 1 s later. With tickets dropped after 2 s no call ever reached the head, and
    the probe saw 0 grants in 60 s with the whole budget free."""
    rng = random.Random(seed)
    now = [0.0]
    ledger = Ledger(1.0, clock=lambda: now[0])
    hold = granted(ledger.reserve(1.0, "run-0"))
    tickets = [short(ledger.reserve(0.5, f"run-{n}")).ticket for n in range(100)]
    ledger.settle(hold, 0.0)
    asks = sorted((rng.uniform(0.0, 4.0), n) for n in range(100))
    settles: list[tuple[float, int]] = []
    grants: list[tuple[float, int]] = []
    while len(grants) < 10 and asks[0][0] < 60.0:
        now[0], n = heapq.heappop(asks)
        while settles and settles[0][0] <= now[0]:
            ledger.settle(heapq.heappop(settles)[1], 0.0)
        answer = ledger.reserve(0.5, f"run-{n}", ticket=tickets[n])
        if isinstance(answer, int):
            waiting = [m for m in range(100) if m not in {g for _, g in grants}]
            assert n <= waiting[0] + 1, f"{n} went ahead of {waiting[:2]}"  # two fit at once
            grants.append((now[0], n))
            heapq.heappush(settles, (now[0] + 1.0, answer))
        else:
            heapq.heappush(asks, (now[0] + rng.uniform(2.1, 4.0), n))
    # Each pair at the head goes as soon as its callers next ask: 10 to 16 s for ten
    # grants across these seeds. 30 s leaves room; the 2 s drop never passed two grants.
    assert len(grants) == 10 and grants[-1][0] < 30.0, grants


def test_a_released_ticket_lets_the_next_call_through() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06))
    first = short(ledger.reserve(0.06, "run-b")).ticket
    ledger.settle(held, 0.0)
    ledger.release(first or 0)
    granted(ledger.reserve(0.06, "run-c"))


@pytest.mark.design("wait.close_keeps_holds")
def test_a_closed_runs_holds_stay_and_are_listed_until_a_late_settle() -> None:
    ledger = Ledger(0.10)
    hold = granted(ledger.reserve(0.06, "run-a", LABEL))
    ticket = short(ledger.reserve(0.06, "run-a")).ticket
    ledger.close_run("run-a")
    assert ledger.reserved == pytest.approx(0.06)  # the call may have been sent
    assert ledger.leaked() == [Held("run-a", LABEL, 0.06, ended=True)]
    assert ticket is not None and not ledger._line  # its ticket went
    ledger.settle(hold, 0.02)  # money spent is spent, even after the close
    assert ledger.leaked() == [] and ledger.spent == pytest.approx(0.02)


# The control server


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ControlServer]:
    made = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=Ledger(0.10))
    made.start()
    made.open_run("run-1", "baseline", {"v": 1})
    yield made
    made.stop()


@pytest.mark.design("wait.protocol")
def test_the_server_answers_at_once_and_names_the_holds(server: ControlServer) -> None:
    talk = ControlClient(server.url, TOKEN)
    held = granted(talk.reserve("run-1", 0.06, LABEL))
    started = time.monotonic()
    refused = short(talk.reserve("run-1", 0.06, LABEL))
    for _ in range(99):
        short(talk.reserve("run-1", 0.06, LABEL, refused.ticket))
    # No request is held open. The first build held each up to 2 s waiting for a settle,
    # so these 100 asks would take 200 s; short requests take far less than 30 s.
    assert time.monotonic() - started < 30
    assert refused.holds == (Held("run-1", LABEL, 0.06),) and refused.ticket is not None
    server.ledger.settle(held, 0.02)
    granted(talk.reserve("run-1", 0.06, LABEL, refused.ticket))


@pytest.mark.parametrize("payload", [{"usd": 0.01, "ticket": "x"}, {"usd": float("nan")}])
def test_a_bad_reserve_is_a_bad_request(server: ControlServer, payload: dict[str, object]) -> None:
    talk = ControlClient(server.url, TOKEN)
    assert talk.request("POST", "/v1/budget/reserve", {"run": "run-1", **payload})[0] == 400
    assert talk.request("POST", "/v1/budget/release", {"ticket": "x"})[0] == 400


def test_a_budget_reply_without_its_numbers_is_a_control_error(server: ControlServer) -> None:
    talk = ControlClient(server.url, TOKEN)
    talk.request = lambda *args: (200, {"ok": False})  # type: ignore[method-assign, assignment]
    with pytest.raises(ControlError, match="budget reply is not valid"):
        talk.reserve("run-1", 0.01)
    talk.request = lambda *args: (_ for _ in ()).throw(ControlError("down"))  # type: ignore[method-assign, assignment]
    talk.release(1)  # best effort: a lost release is dropped after LOST_S, or at the run's end


@pytest.mark.design("wait.run_end_clears_tickets")
def test_closing_a_run_on_the_server_clears_its_tickets(server: ControlServer) -> None:
    """The runner's close, as after a SIGKILL: the run's waiting tickets leave the line."""
    hold = granted(server.ledger.reserve(0.10, "run-0", LABEL))
    assert short(ControlClient(server.url, TOKEN).reserve("run-1", 0.06, LABEL)).ticket
    assert server.ledger._line  # type: ignore[attr-defined]
    server.close_run("run-1")
    assert not server.ledger._line  # type: ignore[attr-defined]
    server.ledger.settle(hold, 0.0)
    granted(server.ledger.reserve(0.10, "run-2"))  # nothing of run-1 is ahead


WAITER = """
import sys, time
from scrimmage_core.client import ControlClient
talk, ticket = ControlClient(sys.argv[1], sys.argv[2]), None
while True:
    answer = talk.reserve("run-1", 0.06, "model:openai", ticket)
    ticket = answer.ticket
    time.sleep(0.1)
"""


@pytest.mark.design("wait.killed_waiter_gets_nothing")
@pytest.mark.slow
def test_a_waiter_killed_mid_wait_is_never_granted_a_hold(server: ControlServer) -> None:
    """The review's leak.py: before, the server granted the dead waiter after a settle."""
    hold = granted(server.ledger.reserve(0.06, "run-0", LABEL))
    child = subprocess.Popen([sys.executable, "-c", WAITER, server.url, TOKEN])
    try:
        until(lambda: bool(server.ledger._line))  # the waiter is waiting, with its ticket
    finally:
        child.kill()
        child.wait()
    assert server.ledger._line  # the dead waiter keeps its place while its run is open
    server.close_run("run-1")
    assert not server.ledger._line  # the run's end clears it, as the runner sees it end
    server.ledger.settle(hold, 0.0)
    time.sleep(0.3)
    assert server.ledger.reserved == 0.0 and server.ledger.leaked() == []


@pytest.mark.design("wait.hint_grows_with_the_line")
def test_a_held_call_is_told_to_ask_later_the_further_back_it_is() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.10, "run-0"))
    tickets = [short(ledger.reserve(0.01, f"run-{n}")) for n in range(60)]
    assert [t.retry_s for t in tickets[:3]] == [0.25, 0.3, 0.35]  # the head asks at once
    assert tickets[10].retry_s == pytest.approx(0.75) and tickets[59].retry_s == 2.0  # capped
    again = short(ledger.reserve(0.01, "run-10", ticket=tickets[10].ticket))
    assert again.retry_s == pytest.approx(0.75)  # asking again keeps the place and its hint
    ledger.release(tickets[0].ticket or 0)
    assert short(ledger.reserve(0.01, "run-1", ticket=tickets[1].ticket)).retry_s == 0.25
    ledger.settle(held, 0.0)
    assert ledger.reserve(0.01, "run-1", ticket=tickets[1].ticket)  # no hint once it fits
    assert budget_module.ASK_MAX_S < LOST_S / 10  # one who follows the hint never goes stale


@pytest.mark.design("wait.line_asks_less")
@pytest.mark.parametrize("seed", range(3))
def test_a_long_line_asks_far_less_than_once_a_quarter_second_each(seed: int) -> None:
    """The ledger's hints and the gate's jittered pause, on a fake clock: 100 calls held for
    60 s. Each asking every 0.25 s would be 400 asks a second, 533 with the jitter's mean."""
    http_gate.JITTER.seed(seed)
    now = [0.0]
    ledger = Ledger(1.0, clock=lambda: now[0])
    granted(ledger.reserve(1.0, "run-0"))  # never settles in this test
    asks: list[tuple[float, int, int | None]] = [(0.0, n, None) for n in range(100)]
    count, head_gaps, last_head = 0, [], None
    while asks[0][0] < 60.0:
        now[0], n, ticket = heapq.heappop(asks)
        answer = short(ledger.reserve(0.5, f"run-{n}", ticket=ticket))
        count += 1
        if n == 0:
            if last_head is not None:
                head_gaps.append(now[0] - last_head)
            last_head = now[0]
        heapq.heappush(asks, (now[0] + http_gate.next_ask_s(answer.retry_s), n, answer.ticket))
    per_s = count / 60.0
    assert per_s < 110, per_s  # about 101 by the sum of the hints
    assert max(head_gaps) <= budget_module.ASK_FIRST_S  # the head is never slowed


@pytest.mark.design("wait.gate_follows_the_hint")
def test_the_gate_pauses_as_long_as_the_runners_hint_says(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget, _ = held_forever(0.004, time_limit=60.0, run="run-other")
    for n in range(10):  # ten calls are ahead of this one
        short(budget.ledger.reserve(0.002, f"run-{n}"))
    clock = Clock(monkeypatch)
    real = budget.reserve
    calls: list[int] = []

    def five_asks(run: str, usd: float, label: str = "", ticket: int | None = None) -> object:
        calls.append(1)
        if len(calls) > 5:
            raise ControlError("enough")  # ends the call: the pauses are what is measured
        return real(run, usd, label, ticket)

    budget.reserve = five_asks  # type: ignore[method-assign, assignment]
    with pytest.raises(ScrimmageRefused):
        client().post(URL, json=BODY)
    hint = budget_module.ask_again_s(10)
    assert len(clock.slept) == 5
    assert all(hint / 2 <= s <= hint for s in clock.slept), (hint, clock.slept)


@pytest.mark.design("wait.many_waiters")
def test_a_hundred_waiters_ask_less_as_the_line_grows_and_get_no_control_errors(
    tmp_path: Path,
) -> None:
    """The review's slots.py, and F2's flood, on the real server.

    Before, waiters filled the 64 slots and connections reset: 16 of 100 got an error. Then
    each asked every 0.25 s, 400 asks a second, and on a loaded machine a settle waited
    behind them, 2 s each. Now each waits as long as the server's hint says, which grows
    with the tickets ahead of it, so the asks are capped by the hints, whatever the load.
    """
    ledger = Ledger(1.0)
    made = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=ledger)
    made.start()
    made.open_run("run-1", "baseline", {"v": 1})
    holds = [granted(ledger.reserve(0.04, "run-0")) for _ in range(20)]  # $0.80 held
    errors: list[str] = []
    asked: list[list[float]] = [[] for _ in range(100)]  # when each waiter asked
    hints: list[float] = [0.0] * 100  # the last hint each waiter was given
    grants: list[int] = []
    stop = threading.Event()

    def waiter(n: int) -> None:
        talk, ticket = ControlClient(made.url, TOKEN), None
        while not stop.is_set():
            asked[n].append(time.monotonic())
            try:
                answer = talk.reserve("run-1", 0.5, LABEL, ticket)
            except ControlError as exc:
                errors.append(str(exc))
                return
            if isinstance(answer, int):
                grants.append(n)
                talk.request("POST", "/v1/budget/settle", {"reservation": answer, "usd": 0.0})
                ticket = None
                time.sleep(http_gate.next_ask_s(None))
            else:
                ticket, hints[n] = answer.ticket, answer.retry_s or 0.0
                time.sleep(http_gate.next_ask_s(answer.retry_s))

    def asks_between(start: float, end: float) -> int:
        return sum(start <= t < end for times in asked for t in times)

    # A waiter waits at least half its hint between asks, so this many asks a second at most.
    most_per_s = sum(2 / budget_module.ask_again_s(place) for place in range(100))
    free: list[int] = []  # the server's free connection slots, sampled

    def sample() -> None:
        while not stop.is_set():
            free.append(made.server.slots._value)  # type: ignore[attr-defined]
            time.sleep(0.005)

    threads = [threading.Thread(target=waiter, args=(n,), daemon=True) for n in range(100)]
    sampler = threading.Thread(target=sample, daemon=True)
    try:
        began = time.monotonic()
        sampler.start()
        for thread in threads:
            thread.start()
        until(lambda: min(map(len, asked)) >= 2 or bool(errors))  # all 100 are waiting
        in_line = list(hints)  # no call can be granted yet, so each keeps its place
        settler = ControlClient(made.url, TOKEN)
        started = time.monotonic()
        statuses = [
            settler.request("POST", "/v1/budget/settle", {"reservation": hold, "usd": 0.0})[0]
            for hold in holds
        ]
        settled = time.monotonic()
        until(lambda: len(grants) >= 10 or bool(errors))  # the freed room goes round them
    finally:
        stop.set()
        for thread in [*threads, sampler]:
            thread.join(30)
        made.stop()
    # The slot bug: a server that held a waiter's request open would fill all 64 slots.
    assert errors == [] and statuses == [200] * 20 and min(free) > 0, min(free)
    # The hints grow with the line: the head asks again at once, the back far less often.
    assert sorted(in_line) == [budget_module.ask_again_s(place) for place in range(100)]
    # Each waiter's first ask, then at most most_per_s; a fixed 0.25 s step would be 400.
    waiting = asks_between(began, started)
    assert waiting <= 100 + most_per_s * (started - began), (waiting, started - began)
    # No flood for the settles to queue behind: at most one ask in flight per waiter, and
    # the rest within the hints.
    during = asks_between(started, settled)
    assert during <= 100 + most_per_s * (settled - started), (during, settled - started)


# The gate


@pytest.mark.design("wait.gate_waits")
def test_a_sync_call_waits_for_another_runs_hold_and_goes_through(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    sink, _ = install(make_plan(budget=0.004), budget)
    other = granted(budget.ledger.reserve(0.003, "run-other", LABEL))
    clock = Clock(monkeypatch, [(0.3, lambda: budget.ledger.settle(other, 0.0001))])
    assert client().post(URL, json=BODY).status_code == 200  # gpt-4o-mini holds about $0.0025
    assert sink.lines[0].get("action") == "real" and "reason" not in sink.lines[0]
    assert clock.waited >= 0.3  # it waited for the settle


@pytest.mark.design("wait.sync_interrupt_gives_back")
def test_a_sync_wait_interrupted_gives_back_its_ticket(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool timeout's SIGALRM, raised 0.6 s into the wait, as the review's probe did.

    The first build kept the ticket, so the run's own next call waited behind it for
    53 s and was refused. Now the wait gives it back, and the next call goes at once.
    """
    budget = Budget(limit=0.004)
    sink, _ = install(make_plan(budget=0.004), budget)
    other = granted(budget.ledger.reserve(0.003, "run-other", LABEL))

    def alarm() -> None:
        raise TimeoutError("the tool's alarm")

    clock = Clock(monkeypatch, [(0.6, alarm)])
    with pytest.raises(TimeoutError):
        client().post(URL, json=BODY)
    until(lambda: not budget.ledger._line)  # type: ignore[attr-defined]  # given back
    line = sink.lines[0]
    assert "TimeoutError" in str(line["error"]) and line["cost_usd"] == 0.0
    budget.ledger.settle(other, 0.0001)
    waited = clock.waited
    assert client().post(URL, json=BODY).status_code == 200
    assert clock.waited == waited  # granted at its first ask, with no wait


@pytest.mark.design("wait.other_run_past_the_short_bound")
def test_a_hold_in_another_run_is_waited_out_past_the_short_bound(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow model in another run must not stop the test at 30 s, here made 0.3 s."""
    monkeypatch.setattr(http_gate, "BUDGET_WAIT_S", 0.3)
    budget = Budget(limit=0.004)
    install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    other = granted(budget.ledger.reserve(0.003, "run-other", LABEL))
    clock = Clock(monkeypatch, [(1.0, lambda: budget.ledger.settle(other, 0.0001))])
    assert client().post(URL, json=BODY).status_code == 200
    assert clock.waited >= 1.0  # past the short bound of 0.3 s


@pytest.mark.design("wait.bounds")
def test_the_bound_is_short_for_own_holds_and_long_for_other_runs() -> None:
    plan = make_plan(time_limit_s=120.0)
    assert http_gate.budget_wait(plan) == 30.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0)) == 5.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), elapsed=4.0) == 3.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), elapsed=11.0) == 0.0
    assert http_gate.budget_wait(plan, elapsed=20.0, own=False) == 90.0  # 10 s left over
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), own=False) == 9.0
    assert http_gate.budget_wait(plan, elapsed=200.0, own=False) == 0.0


@pytest.mark.design("wait.counts_from_the_runners_start")
@pytest.mark.parametrize(("elapsed", "bound"), [(0.0, 110.0), (50.0, 60.0)])
def test_a_wait_on_another_run_shrinks_by_the_time_the_runner_says_has_passed(
    gated: None, monkeypatch: pytest.MonkeyPatch, elapsed: float, bound: float
) -> None:
    """The runner's run_timeout counts from its start. A program that took 50 s to load has
    70 s of its 120 s left, so its wait ends 10 s before the limit, at 60 s, not at 110 s."""
    budget = Budget(limit=0.004)
    budget.ledger.reserve(0.003, "run-other", LABEL)  # a hold that never settles
    before = time.monotonic()
    sink, _ = install(make_plan(budget=0.004, time_limit_s=120.0, elapsed_s=elapsed), budget)
    clock = Clock(monkeypatch)
    clock.start = clock.now = time.monotonic()  # the fake clock goes on from the real one
    took = clock.start - before  # the plan arrived in this window
    with pytest.raises(ScrimmageRefused, match="model:openai in run-other"):
        client().post(URL, json=BODY)
    assert bound - took - 0.001 <= clock.waited <= bound + 0.001
    numbers = sink.lines[0]["budget"]
    assert isinstance(numbers, dict) and numbers["own"] is False


def held_forever(
    limit: float, time_limit: float = 1.0, run: str = "run-1"
) -> tuple[Budget, list[object]]:
    budget = Budget(limit=limit)
    budget.ledger.reserve(0.003, run, LABEL)  # a hold that never settles
    sink, _ = install(make_plan(budget=limit, time_limit_s=time_limit), budget)
    return budget, sink.lines  # type: ignore[return-value]


@pytest.mark.design("wait.gate_refuses_held")
def test_a_call_held_out_by_its_own_run_is_refused_naming_the_hold(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget, lines = held_forever(0.004)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    assert clock.waited == pytest.approx(0.5, abs=0.001)  # half of a one-second time limit
    message = str(caught.value)
    for part in (
        "$0.0030 is held by calls in flight, of a budget of $0.004 "
        "(model:openai in this run $0.0030)",
        "$0.0000 is spent",
        "budget of $0.004",
        "a stream left open, or not read to its end, keeps its hold",
    ):
        assert part in message, message
    line = lines[0]
    assert isinstance(line, dict) and line["reason"] == "budget_held"
    numbers = line["budget"]
    assert isinstance(numbers, dict) and numbers["held"] == pytest.approx(0.003)
    assert numbers["holds"] == [{"run": "run-1", "label": LABEL, "usd": 0.003, "ended": False}]
    assert numbers["limit"] == 0.004 and numbers["waited_s"] == 0.5
    assert budget.ledger.reserve(0.0, "run-x") is not None and not budget.ledger._line


def test_a_refusal_after_other_runs_names_them_and_gives_the_parallel_advice(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_gate, "RUN_MARGIN_S", 0.5)
    _, _ = held_forever(0.004, time_limit=1.0, run="run-other")
    Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    message = str(caught.value)
    assert "model:openai in run-other $0.0030" in message
    assert "lower [agent] parallel" in message and "reserve_output_tokens (4096)" in message


@pytest.mark.design("wait.in_steps")
def test_a_wait_asks_again_every_step_with_one_ticket(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(budget_module, "ASK_FIRST_S", 0.05)  # the runner's hint at the head
    budget, _ = held_forever(0.004, time_limit=1.0)
    Clock(monkeypatch)
    asked: list[int | None] = []
    real = budget.reserve

    def watch(run: str, usd: float, label: str = "", ticket: int | None = None) -> object:
        asked.append(ticket)
        return real(run, usd, label, ticket)

    budget.reserve = watch  # type: ignore[method-assign, assignment]
    with pytest.raises(ScrimmageRefused, match=r"waited [01]s"):
        client().post(URL, json=BODY)
    assert len(asked) >= 5 and asked[0] is None and len(set(asked[1:])) == 1


@pytest.mark.design("wait.gate_refuses_spent")
def test_a_call_spent_money_cannot_cover_is_refused_at_once(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    budget.ledger.settle(granted(budget.ledger.reserve(0.003)), 0.003)
    sink, _ = install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused, match=r"\$0.0030 is spent"):
        client().post(URL, json=BODY)
    assert clock.slept == []  # no pause: waiting cannot help
    assert sink.lines[0]["reason"] == "budget"


class GateWaits:
    """http_gate's asyncio, with its sleep noted: the gate's pause between asks.

    Each pause is a window, from its start to its end, as a count of the ticks so far.
    """

    def __init__(self, ticks: list[int]) -> None:
        self.ticks = ticks
        self.windows: list[tuple[int, int]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        began = len(self.ticks)
        await asyncio.sleep(seconds)
        self.windows.append((began, len(self.ticks)))


@pytest.mark.design("wait.loop_runs")
def test_an_async_call_waits_without_blocking_the_loop(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    budget = Budget(limit=0.004)
    other = granted(budget.ledger.reserve(0.003, "run-other"))
    install(make_plan(budget=0.004), budget)
    ticks: list[int] = []
    waits = GateWaits(ticks)
    monkeypatch.setattr(http_gate, "asyncio", waits)
    done = asyncio.Event()

    async def tick() -> None:
        while not done.is_set():
            ticks.append(len(ticks))
            await asyncio.sleep(0.01)
            if len(waits.windows) >= 3:
                # The settle comes from a task on the loop, after three of the gate's waits.
                budget.ledger.settle(other, 0.0)

    async def main() -> int:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            ticker = asyncio.create_task(tick())
            reply = await session.post(URL, json=BODY)
            done.set()
            await ticker
            return reply.status_code

    assert asyncio.run(main()) == 200
    # The gate paused on the loop between asks, and the ticker ran inside every pause. A
    # wait that blocked the loop, such as time.sleep, makes no window or an empty one.
    assert len(waits.windows) >= 3
    assert all(end > began for began, end in waits.windows), waits.windows


@pytest.mark.design("wait.cancel_ends_it")
def test_a_task_cancelled_while_it_waits_stops_and_gives_up_its_ticket(gated: None) -> None:
    import httpx2

    budget, lines = held_forever(0.004, time_limit=60.0, run="run-other")
    asks: list[int | None] = []
    real = budget.reserve

    def watch(run: str, usd: float, label: str = "", ticket: int | None = None) -> object:
        asks.append(ticket)
        return real(run, usd, label, ticket)

    budget.reserve = watch  # type: ignore[method-assign, assignment]
    at_cancel: list[int] = []

    async def main() -> None:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            task = asyncio.create_task(session.post(URL, json=BODY))
            for _ in range(6000):  # until the gate has asked once and is waiting
                if asks:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            at_cancel.append(len(asks))
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())  # it waits for the gate's thread, so the wait has ended
    assert asks and len(asks) - at_cancel[0] <= 1  # an ask in flight at most, then it stopped
    assert budget.ledger.reserved == pytest.approx(0.003)  # only the other hold is left
    assert not budget.ledger._line  # type: ignore[attr-defined]  # its ticket is gone
    line = lines[0]
    assert isinstance(line, dict) and "CancelledError" in str(line["error"])


# End to end


def agent(project: Project, function: str, settings: str) -> None:
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\n{settings}')
    project.edit(EXAMPLE_FAULT, "")


def run_on_fake(project: Project, fixtures: list[str]) -> Result:
    fake = FakeOpenAI(fixtures)
    fake.start()
    try:
        return project.test(fake)
    finally:
        fake.stop()


@pytest.mark.design("wait.parallel_runs_end_to_end")
@pytest.mark.slow
def test_parallel_runs_whose_holds_overlap_all_answer(project: Project) -> None:
    # gpt-4o-mini holds 4096 x $0.60 a million, about $0.0025, so two fit in $0.006.
    agent(
        project, "acme.rag:answer_streamed_slowly", "runs = 4\nparallel = 4\nmax_cost_usd = 0.006"
    )
    result = run_on_fake(project, ["chat_stream_usage"])
    assert result.finished, result.out
    assert [s["outcome"] for s in result.summaries()] == ["answered"] * 4
    assert "could not be reserved" not in result.out


DEADLOCK = '''"""The agent that holds a stream open, with a short wait for its own holds."""

from scrimmage_core import http_gate

from acme.rag import answer_holding_a_stream as answer

# The bound under test, made short in this run's own process. The run's time limit stays
# at 120 s, so the wait always ends first, however slowly the run starts.
http_gate.BUDGET_WAIT_S = 1.0
__all__ = ["answer"]
'''


@pytest.mark.design("wait.deadlock_end_to_end")
@pytest.mark.slow
def test_a_stream_held_open_across_a_second_call_ends_in_a_clear_stop(project: Project) -> None:
    (project.root / "acme" / "deadlock.py").write_text(DEADLOCK)
    agent(project, "acme.deadlock:answer", "runs = 1\nmax_cost_usd = 0.004")
    result = run_on_fake(project, ["chat_stream_usage"])
    assert result.code == 2, result.out
    assert [s["outcome"] for s in result.summaries()] == ["not reserved"]
    assert "test mistake: a model call could not be reserved" in result.out
    assert "was held by calls in flight (model:openai in run-" in result.out
    assert "after a wait of 1s for calls in flight" in result.out, result.out
    assert "was reached" not in result.out


SLOW_START = """\
# The first run takes the budget. The second starts its program once the first holds it.
if mkdir first 2>/dev/null; then exec python race.py hold "$1"; fi
while [ ! -e holding ]; do sleep 0.05; done
exec python race.py wait "$1"
"""

RACE = '''"""Run A opens a stream, which holds the budget, and keeps it open. Run B needs room."""

import sys
import time
from pathlib import Path

import openai

role, question = sys.argv[1], sys.argv[2]
client = openai.OpenAI(max_retries=0)
messages = [{"role": "user", "content": question}]
if role == "hold":
    client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
    Path("holding").touch()
    time.sleep(120)  # the stream stays open until the test stops this run
else:
    reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    print(reply.choices[0].message.content)
'''
STARTUP_S = 110.0  # run_timeout less its 10 s margin: no time is left to wait


@pytest.mark.design("wait.slow_start_end_to_end")
@pytest.mark.slow
def test_a_slow_start_up_leaves_no_wait_past_the_runners_limit(
    project: Project, monkeypatch: pytest.MonkeyPatch, time_limit: TimeLimit
) -> None:
    """A program that takes 110 s to load, as `uv run` on a cold cache can, then waits on
    another run's hold. Its wait must end before the runner's limit, so the run reads "not
    reserved", not "timed out".

    No real time passes: each run is made STARTUP_S old when its program asks for its plan.
    Only the 10 s margin is then left, so any wait at all is one the design forbids, and the
    limit arrives the moment a call asks again. The first build waited, and timed out.
    """
    real_plan = control.ControlServer._plan

    def slow_start(server: control.ControlServer, run: str) -> tuple[int, dict[str, object]]:
        server.runs[run].opened = time.monotonic() - STARTUP_S
        return real_plan(server, run)

    real_reserve = control.ControlServer._reserve
    asked_again: list[str] = []

    def reserve(server: control.ControlServer, body: bytes) -> tuple[int, dict[str, object]]:
        if json.loads(body).get("ticket") is not None:
            asked_again.append("waiting")
        return real_reserve(server, body)

    monkeypatch.setattr(control.ControlServer, "_plan", slow_start)
    monkeypatch.setattr(control.ControlServer, "_reserve", reserve)
    time_limit.at(lambda: bool(asked_again))
    (project.root / "slow_start.sh").write_text(SLOW_START)
    (project.root / "race.py").write_text(RACE)
    project.edit(
        'function = "acme.agent:answer"',
        'command = "sh slow_start.sh {question}"\nruns = 2\nparallel = 2\nmax_cost_usd = 0.004',
    )
    project.edit(EXAMPLE_FAULT, "")
    result = run_on_fake(project, ["chat_stream_usage"])
    assert result.code == 2, result.out
    assert sorted(str(s["outcome"]) for s in result.summaries()) == ["not reserved", "stopped"]
    assert "test mistake: a model call could not be reserved" in result.out
    assert asked_again == [] and True not in time_limit.reached


@pytest.mark.design("wait.leak_reported")
@pytest.mark.slow
def test_a_run_killed_with_a_hold_leaves_it_held_and_the_report_says_so(
    project: Project,
) -> None:
    """The agent kills itself once its stream holds money, so no timeout is raced."""
    agent(project, "acme.rag:answer_killed_holding", "runs = 1")
    result = run_on_fake(project, ["chat_stream_usage"])
    assert re.search(r"warning: run-\w+ ended with model:openai still holding \$0\.00", result.out)


@pytest.mark.design("wait.async_holds_no_thread")
@pytest.mark.slow
def test_many_async_calls_to_a_host_name_all_answer_under_a_tight_budget(
    project: Project,
) -> None:
    """The verifier's repro: 16 calls by gather to localhost, with room for 2 at a time.
    Before, waiting calls held asyncio's threads, which also look up host names."""
    agent(project, "acme.rag:answer_many_async", "runs = 1\nmax_cost_usd = 0.006")
    project.edit(
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}, '
        '{host = "localhost", provider = "openai"}]',
    )
    result = run_on_fake(project, ["chat_stream_usage"])
    assert [s["outcome"] for s in result.summaries()] == ["answered"], result.out
    assert result.summaries()[0]["answer"] == " ".join(["Hello"] * 16)
    model = [x for x in result.records()[0] if x.get("kind") == "model"]
    assert len(model) == 16 and not any(x.get("error") for x in model)


def short_of(holds: tuple[Held, ...], spent: float = 0.0) -> Shortfall:
    held = sum(h.usd for h in holds)
    return Shortfall("held", 0.0025, spent, held, 0.006, 1, holds)


@pytest.mark.design("wait.own_run_classified")
def test_a_call_waits_on_itself_when_its_own_holds_alone_keep_it_out() -> None:
    own, other = Held("run-1", LABEL, 0.004), Held("run-2", LABEL, 0.001)
    assert http_gate.waits_on_itself(short_of((own, other)), "run-1")  # other run or not
    assert not http_gate.waits_on_itself(short_of((Held("run-1", LABEL, 0.001), other)), "run-1")
    assert not http_gate.waits_on_itself(short_of((Held("run-2", LABEL, 0.004),)), "run-1")
    assert http_gate.waits_on_itself(short_of((own,), spent=0.0001), "run-1")


@pytest.mark.design("wait.own_run_with_another_run_holding")
def test_an_own_run_deadlock_ends_at_the_short_bound_though_another_run_holds_money(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_gate, "BUDGET_WAIT_S", 0.3)
    budget = Budget(limit=0.006)
    budget.ledger.reserve(0.004, "run-1", LABEL)  # this run's open stream
    budget.ledger.reserve(0.001, "run-other", LABEL)  # another run's call, also in flight
    sink, _ = install(make_plan(budget=0.006, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused, match="may be waiting on itself"):
        client().post(URL, json=BODY)
    assert clock.waited == pytest.approx(0.3, abs=0.001)  # the short bound, not most of a minute
    numbers = sink.lines[0]["budget"]
    assert isinstance(numbers, dict) and numbers["own"] is True and numbers["waited_s"] == 0.3


@pytest.mark.design("wait.ended_run_counts_as_spent")
def test_a_hold_left_by_an_ended_run_refuses_at_once_and_is_named(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    budget.ledger.reserve(0.003, "run-dead", LABEL)
    budget.ledger.close_run("run-dead")  # killed during its call: the hold never settles
    sink, _ = install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    assert clock.slept == []  # waiting cannot help
    message = str(caught.value)
    assert "left by run-dead, which ended while still holding" in message
    assert "waiting cannot help" in message and "waiting on itself" not in message
    assert "lower [agent] parallel" not in message
    line = sink.lines[0]
    assert line["reason"] == "budget"
    numbers = line["budget"]
    assert isinstance(numbers, dict) and numbers["holds"][0]["ended"] is True  # type: ignore[index]


def test_the_ledger_tells_a_left_hold_from_one_in_flight() -> None:
    ledger = Ledger(0.10)
    granted(ledger.reserve(0.06, "run-dead", LABEL))
    ledger.close_run("run-dead")
    refused = short(ledger.reserve(0.06, "run-b"))
    assert refused.why == "left" and refused.ticket is None
    assert refused.holds == (Held("run-dead", LABEL, 0.06, ended=True),)
    granted(ledger.reserve(0.03, "run-b"))  # room that is free is still free


@pytest.mark.design("wait.many_fit_at_once")
def test_fifty_waiters_that_all_fit_are_granted_together(server: ControlServer) -> None:
    """The verifier's drain.py: before, only the head was granted per ask, so 50 took 3.8 s."""
    server.ledger.limit_usd = 1.0
    hold = granted(server.ledger.reserve(0.99, "run-0", "big"))
    asks = [0] * 50
    grants: list[int] = []
    held_after: list[int] = []  # a waiter that asked after the settle and still waited
    settled = threading.Event()

    def waiter(n: int) -> None:
        talk, ticket = ControlClient(server.url, TOKEN), None
        for _ in range(400):
            after = settled.is_set()
            answer = talk.reserve("run-1", 0.01, LABEL, ticket)
            asks[n] += 1
            if isinstance(answer, int):
                grants.append(n)
                return
            if after:
                held_after.append(n)
            ticket = answer.ticket
            time.sleep(http_gate.WAIT_STEP_S)

    threads = [threading.Thread(target=waiter, args=(n,), daemon=True) for n in range(50)]
    for thread in threads:
        thread.start()
    until(lambda: min(asks) >= 1)  # all 50 wait behind the big hold
    server.ledger.settle(hold, 0.0)
    settled.set()
    for thread in threads:
        thread.join(60)
    # Each is granted at its first ask after the settle. Before, only the head of the line
    # was granted per ask, so the last of 50 waited through dozens of asks.
    assert len(grants) == 50 and held_after == []


def test_waiters_ahead_that_would_fit_are_served_together_in_order() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.10, "run-0"))
    tickets = [short(ledger.reserve(0.03, f"run-{n}")).ticket for n in (1, 2, 3, 4)]
    ledger.settle(held, 0.0)
    granted(ledger.reserve(0.03, "run-3", ticket=tickets[2]))  # 1 and 2 still fit before it
    assert short(ledger.reserve(0.03, "run-4", ticket=tickets[3])).why == "held"  # no room left


@pytest.mark.design("wait.report_says_reached_only_when_spent")
def test_the_report_says_reached_only_when_money_spent_reached_the_limit() -> None:
    from scrimmage_core.runner import budget_line, reached

    agent_ = config_agent(parallel=4)
    numbers: dict[str, object] = {
        "usd": 0.0331,
        "spent": 0.0001,
        "held": 0.0993,
        "limit": 0.1,
        "waited_s": 0.0,
        "own": False,
        "holds": [{"run": "run-7", "label": LABEL, "usd": 0.0331, "ended": False}],
    }
    line = budget_line("budget", numbers, agent_)
    assert line.startswith("a model call could not be reserved: it could cost up to $0.0331")
    assert "$0.0001 was spent and $0.0993 was held by calls in flight" in line
    assert "(model:openai in run-7 $0.0331), of a budget of $0.1" in line
    assert "reserve_output_tokens (4096)" in line and "was reached" not in line
    full = {**numbers, "spent": 0.1003}
    assert budget_line("budget", full, agent_).startswith("the budget of $0.1 was reached: $0.1003")
    held = budget_line("budget_held", {**numbers, "waited_s": 30.0}, agent_)
    assert "after a wait of 30s for calls in flight" in held
    assert reached(full) and not reached(numbers)


@pytest.mark.design("wait.report_advice_fits")
@pytest.mark.parametrize("parallel", [1, 2])
def test_the_reports_advice_fits_the_case_and_the_parallel_setting(parallel: int) -> None:
    from scrimmage_core.runner import budget_line

    base: dict[str, object] = {"usd": 0.03, "spent": 0.0, "held": 0.08, "limit": 0.1}
    hold = {"run": "run-7", "label": LABEL, "usd": 0.08, "ended": False}
    others = budget_line("budget_held", {**base, "holds": [hold]}, config_agent(parallel))
    assert ("lower [agent] parallel" in others) is (parallel > 1)
    own = budget_line("budget_held", {**base, "own": True}, config_agent(parallel))
    assert "close a stream before the next call" in own and "parallel" not in own
    dead = budget_line(
        "budget", {**base, "holds": [{**hold, "ended": True}]}, config_agent(parallel)
    )
    assert "left by run-7, which ended while still holding" in dead
    assert "parallel" not in dead and "run killed during a call" in dead


def config_agent(parallel: int) -> Agent:
    from scrimmage_core import config

    loaded = config.load(Path(__file__).parent / "example" / "scrimmage.toml").agent
    return dataclasses.replace(loaded, parallel=parallel)


@pytest.mark.design("wait.numbers_from_the_stopping_run")
def test_the_report_takes_its_numbers_from_the_run_that_stopped_the_test() -> None:
    from scrimmage_core.runner import RunResult, budget_refusal, stop_numbers

    def result(reason: str, spent: float) -> RunResult:
        made = RunResult("run", None, "not reserved", None, None, {})  # type: ignore[arg-type]
        made.budget_reason, made.budget = reason, {"spent": spent}
        return made

    results = [result("budget", 0.01), result("budget_held", 0.02)]
    assert stop_numbers(results, "budget_held") == {"spent": 0.02}
    assert stop_numbers(results, "no_price") is None
    lines: list[dict[str, object]] = [
        {"action": "refused", "reason": "unlisted"},
        {"action": "refused", "reason": "budget_held", "budget": {"spent": 0.5}},
    ]
    assert budget_refusal(lines) == lines[1]  # its first budget refusal, not its first refusal
