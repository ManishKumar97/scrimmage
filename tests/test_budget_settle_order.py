"""A finished call's real cost counts before the next call is reserved."""

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from scrimmage import http_gate, state
from scrimmage.budget import Ledger
from scrimmage.client import ControlClient, Sender
from scrimmage.control import ControlServer
from scrimmage.errors import ScrimmageRefused
from scrimmage.records import RecordStore
from test_http_gate import URL, make_plan

TOKEN = "d" * 64
LIMIT = 0.06
OUTPUT = 5000  # gpt-4.1 output tokens in each reply: $0.04, far above a hold of 64 tokens
SENT = {"model": "gpt-4.1", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
REPLY = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": OUTPUT}}
EACH = (10 * 2.00 + OUTPUT * 8.00) / 1e6  # gpt-4.1: $2 in and $8 out per million


class Stack:
    """The real runner ledger, control server, client and sender, with the sender thread
    never started: nothing but the gate's own steps delivers a settle, so the timing that
    a fast agent meets by luck is the same on every run."""

    def __init__(self, tmp_path: Path) -> None:
        self.ledger = Ledger(LIMIT)
        self.control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=self.ledger)
        self.control.start()
        self.control.open_run("run-1", "baseline", {"v": 1})
        self.client = ControlClient(self.control.url, TOKEN)
        self.sender = Sender(self.client, "run-1")
        plan = make_plan(budget=LIMIT, reserve_output_tokens=64)
        run = state.RunState(plan, self.sender, [])
        state.install(run)
        ctx = http_gate.Context(run, self.client, self.sender, list[str]().append)
        http_gate.patch(httpx2, ctx)
        answer = httpx2.MockTransport(lambda _: httpx2.Response(200, json=REPLY))
        self.http = httpx2.Client(transport=answer)
        self.holds: list[float] = []
        reserve = self.ledger.reserve

        def watch(usd: float, *rest: object) -> object:
            self.holds.append(usd)
            return reserve(usd, *rest)  # type: ignore[arg-type]

        self.ledger.reserve = watch  # type: ignore[method-assign]

    def calls(self, count: int) -> int:
        """Make calls one at a time until one is refused; how many were made."""
        for made in range(count):
            try:
                self.http.post(URL, json=SENT)
            except ScrimmageRefused:
                return made
        return count

    def stop(self) -> None:
        self.control.stop()
        state.install(None)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    stack = Stack(tmp_path)
    yield stack
    stack.stop()


@pytest.mark.xfail(strict=True, reason="a settle is sent up to 0.1 s after its call")
def test_one_call_at_a_time_passes_the_limit_by_at_most_one_calls_excess(stack: Stack) -> None:
    """The docs' bound: only calls in flight at once can pass max_cost_usd, each by what it
    cost above its hold. Here one call is in flight at a time."""
    made = stack.calls(6)
    excess = EACH - min(stack.holds)
    # The sender never ran, so the ledger has seen no settle yet: what the calls cost is
    # what the mock server billed them, EACH a call.
    assert made * EACH <= LIMIT + excess, f"{made} calls cost ${made * EACH:.4f} of ${LIMIT}"
