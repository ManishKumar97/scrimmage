"""The decision engine: for one call in one run, fault, mock, real or refused.

A pure function of the call, the plan and the call numbers the counters gave it. The
gate counts the call first, then asks. Faults win over mocks, because the baseline row
uses the mocks alone and each fault row adds one fault.
"""

import threading
from dataclasses import dataclass, field
from typing import Literal, cast

from scrimmage import capture
from scrimmage.config import READ_METHODS as READ_METHODS
from scrimmage.config import Fault, Mock, carried
from scrimmage.labels import matches, matches_real
from scrimmage.plan import Plan

Action = Literal["fault", "mock", "real", "refused"]
Kind = Literal["tool", "model", "http", "mcp", "process"]
# The reason of a call a gate refused because the test asks what cannot be done, such as
# empty on a tool with no empty value. The test is wrong, not the agent: no refused call.
MISTAKE_REFUSAL = "mistake"


@dataclass(frozen=True)
class Call:
    """What a gate saw.

    For a model call, retry is the library's own attempt number (0 for a first attempt),
    key ties a retry to its first attempt: who sent it and what it sent, and model is the
    name in its body, or None when none can be read. For a web request, name is the path.
    For an MCP tool, the label names the server too and short is the label without it.
    For a tool or MCP call, args is its arguments as a mock with args holds them, set only
    when the plan has such a mock or is recording; None when they are not known or cannot
    be held. marked is False for a model call whose library does not number its attempts,
    so its retries cannot be told from new requests.
    """

    kind: Kind
    label: str
    name: str
    provider: str | None = None
    method: str | None = None
    retry: int = 0
    key: str | None = None
    short: str | None = None
    model: str | None = None
    args: object = None
    marked: bool = True


@dataclass(frozen=True)
class Decision:
    action: Action
    fault: int | None = None  # index into plan.faults
    mock: int | None = None  # index into plan.mocks
    n: int = 0  # the fault's number for this call: its attempt, or its request
    reason: str | None = None  # why a call was refused
    # A carried fault (append, replace, slow) lets the call happen: what the call gets beneath
    # it, the baseline's own answer, and mock is then that answer's mock.
    under: Literal["real", "mock"] | None = None


def labelled(entry: Mock | Fault, call: Call) -> bool:
    """Whether a mock or fault names this call, leaving a model's name out of it."""
    if not matches(entry.call, call.label) and entry.call != call.short:
        return False
    if entry.method is not None and entry.method != call.method:
        return False
    return entry.path is None or entry.path == call.name


def on_label(mock: Mock, call: Call) -> bool:
    """Whether a mock is on this call's label, whatever its arguments: also an MCP tool in
    its short form."""
    return matches(mock.call, call.label) or mock.call == call.short


def listed_by_name(call: Call, plan: Plan) -> bool:
    """Whether the team named this label under real. A wildcard does not count."""
    return call.label in plan.real or call.short in plan.real


def names(entry: Mock | Fault, call: Call) -> bool:
    """Whether a mock or fault names this call exactly as it is written.

    A mock with args names only a call whose arguments are type-strict equal to them; any
    other call goes on to the next mock and then to the normal rule.
    """
    if not labelled(entry, call):
        return False
    if isinstance(entry, Mock):
        return entry.args is None or (
            call.args is not None and capture.differ(call.args, entry.args, "args") is None
        )
    return entry.model in (None, call.model)


def unknowable(call: Call, plan: Plan) -> bool:
    """A fault names a model, its label covers this call, and this call has no readable name.

    Firing could hit the wrong call and passing could spare the right one, so neither is safe.
    """
    if call.model is not None:
        return False
    return any(fault.model is not None and labelled(fault, call) for fault in plan.faults)


def schedule(when: str) -> tuple[str, int]:
    """("always", 0), ("first", N), ("requests", N) or ("call", N)."""
    if when == "always":
        return "always", 0
    kind, _, n = when.partition(" ")
    if kind not in ("first", "requests", "call") or not n.isdigit():
        raise ValueError(f"schedule {when!r} is not a schedule")
    return kind, int(n)


def fires(when: str, n: int) -> bool:
    """Whether a schedule fires on the n-th matching call (or request)."""
    kind, limit = schedule(when)
    if kind == "always":
        return n >= 1
    if kind == "call":
        return n == limit
    return 1 <= n <= limit


@dataclass
class Counters:
    """One set of counters per run, shared by every thread and task, behind a lock.

    "first N", "call N" and "always" count attempts. "requests N" counts requests: an attempt the
    library marks as a retry gets the number of the request it retries.
    """

    plan: Plan
    attempts: list[int] = field(init=False)
    requests: list[int] = field(init=False)
    request_of: list[dict[str, int]] = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        size = len(self.plan.faults)
        self.attempts = [0] * size
        self.requests = [0] * size
        self.request_of = [{} for _ in range(size)]

    def note(self, call: Call) -> list[int]:
        """Count the call for every fault that names it; this call's number per fault."""
        numbers = [0] * len(self.plan.faults)
        with self.lock:
            for i, fault in enumerate(self.plan.faults):
                if not names(fault, call):
                    continue
                if schedule(fault.when)[0] != "requests":
                    self.attempts[i] += 1
                    numbers[i] = self.attempts[i]
                    continue
                seen = self.request_of[i]
                if call.retry > 0 and call.key is not None and call.key in seen:
                    numbers[i] = seen[call.key]
                else:
                    self.requests[i] += 1
                    numbers[i] = self.requests[i]
                    if call.key is not None:
                        seen[call.key] = numbers[i]
        return numbers


def decide(call: Call, plan: Plan, numbers: list[int]) -> Decision:
    for i, fault in enumerate(plan.faults):
        if names(fault, call) and fires(fault.when, numbers[i]):
            if not carried(fault.do):
                return Decision("fault", fault=i, n=numbers[i])
            beneath = settled(call, plan)  # the rest of decide: what the baseline gives the call
            if beneath.action == "refused":
                return beneath  # a fault never makes a refused call run
            under: Literal["real", "mock"] = "mock" if beneath.action == "mock" else "real"
            return Decision("fault", fault=i, n=numbers[i], mock=beneath.mock, under=under)
    return settled(call, plan)


def settled(call: Call, plan: Plan) -> Decision:
    """What a call gets when no fault takes it over: a mock, real, or a refusal."""
    if unknowable(call, plan):
        return Decision("refused", reason="unreadable_model")
    if call.kind == "model":
        return Decision("real")  # model calls are what is being tested; never mocked
    for i, mock in enumerate(plan.mocks):
        if names(mock, call):
            return Decision("mock", mock=i)
    if not listed_by_name(call, plan) and any(
        mock.args is not None and on_label(mock, call) for mock in plan.mocks
    ):
        return Decision("refused", reason="unlisted")
    path = call.name if call.kind == "http" else None
    if matches_real(list(plan.real), call.label, call.method, path) or call.short in plan.real:
        return Decision("real")
    if call.kind == "http":
        if call.method in READ_METHODS:
            return Decision("real")
        return Decision("refused", reason="web_write")
    return Decision("refused", reason="unlisted")


def beneath(decision: Decision) -> Decision:
    """The decision a carried fault sits on: what the call gets, as the baseline gives it."""
    if decision.under is None:
        return decision
    return Decision(decision.under, mock=decision.mock)


@dataclass(frozen=True)
class Miss:
    """A call that had a mock on its label and no mock named it, for the report.

    nearest is the index of the mock it came closest to, for display only: it never decides
    a match. differs names the top-level arguments that differ (tool or MCP), or "method"
    and "path" (web). known is False when the call's arguments could not be read.
    """

    reason: str  # arguments, request, no_mock or unreadable
    nearest: int | None = None
    differs: tuple[str, ...] = ()
    known: bool = False


def differing_keys(seen: dict[str, object], mock: dict[str, object]) -> tuple[str, ...]:
    """The top-level keys that are missing, added or not type-strict equal."""
    keys = [*seen, *(k for k in mock if k not in seen)]
    return tuple(
        k for k in keys if k not in seen or k not in mock or capture.differ(seen[k], mock[k], k)
    )


def alike(mock_path: str | None, path: str) -> int:
    """How many leading path segments a mock's path and a request's path share."""
    count = 0
    for a, b in zip((mock_path or "").split("/"), path.split("/"), strict=False):
        if a != b:
            break
        count += 1
    return count


def missed(call: Call, plan: Plan, decision: Decision) -> Miss | None:
    """Whether a decided call is a miss, and the nearest mock. Pure; it reads the decision
    the engine just gave, before a gate changes it.

    A model call, a process call, a call a fault or a mock answered and a call after a
    refusal are never a miss.
    """
    if decision.action not in ("real", "refused") or call.kind not in ("tool", "mcp", "http"):
        return None
    if decision.reason not in (None, "unlisted", "web_write"):
        return None
    if call.kind == "http":
        found = [(i, m) for i, m in enumerate(plan.mocks) if m.call == call.label]
        if not found:
            return None
        best = min(
            found,
            key=lambda x: (x[1].method not in (None, call.method), -alike(x[1].path, call.name)),
        )[0]
        mock = plan.mocks[best]
        differs = tuple(
            name
            for name, ok in (
                ("method", mock.method in (None, call.method)),
                ("path", mock.path in (None, call.name)),
            )
            if not ok
        )
        return Miss("request", best, differs, True)
    found = [(i, m) for i, m in enumerate(plan.mocks) if m.args is not None and on_label(m, call)]
    if not found:
        # A refused call with no mock of its own is reported only when the test pins
        # arguments somewhere, so a clean replay's ordinary fall-through says nothing.
        if decision.action == "refused" and any(m.args is not None for m in plan.mocks):
            return Miss("no_mock", None, (), isinstance(call.args, dict))
        return None
    held: object = call.args
    if not isinstance(held, dict):
        return Miss("arguments", found[0][0], (), False)
    seen = cast(dict[str, object], held)
    scored = [(i, differing_keys(seen, m.args or {})) for i, m in found]
    best, differs = min(scored, key=lambda x: len(x[1]))
    return Miss("arguments", best, differs, True)
