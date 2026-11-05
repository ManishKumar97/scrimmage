"""The decision engine: for one call in one run, fault, mock, real or refused.

A pure function of the call, the plan and the call numbers the counters gave it. The
gate counts the call first, then asks. Faults win over mocks, because the baseline row
uses the mocks alone and each fault row adds one fault.
"""

import threading
from dataclasses import dataclass, field
from typing import Literal

from faultline_sdk.labels import matches, matches_real
from faultline_sdk.plan import Plan

Action = Literal["fault", "mock", "real", "refused"]
Kind = Literal["tool", "model", "http"]
READ_METHODS = ("GET", "HEAD")


@dataclass(frozen=True)
class Call:
    """What a gate saw.

    For a model call, retry is the library's own attempt number (0 for a first attempt)
    and key ties a retry to its first attempt: who sent it and what it sent.
    """

    kind: Kind
    label: str
    name: str
    provider: str | None = None
    method: str | None = None
    retry: int = 0
    key: str | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    fault: int | None = None  # index into plan.faults
    mock: int | None = None  # index into plan.mocks
    n: int = 0  # the fault's number for this call: its attempt, or its request
    reason: str | None = None  # why a call was refused


def schedule(when: str) -> tuple[str, int]:
    """("always", 0), ("first", N) or ("requests", N)."""
    if when == "always":
        return "always", 0
    kind, _, n = when.partition(" ")
    if kind not in ("first", "requests") or not n.isdigit():
        raise ValueError(f"schedule {when!r} is not supported yet")
    return kind, int(n)


def fires(when: str, n: int) -> bool:
    """Whether a schedule fires on the n-th matching call (or request)."""
    kind, limit = schedule(when)
    return n >= 1 if kind == "always" else 1 <= n <= limit


@dataclass
class Counters:
    """One set of counters per run, shared by every thread and task, behind a lock.

    "first N" and "always" count attempts. "requests N" counts requests: an attempt the
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
                if not matches(fault.call, call.label):
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
        if matches(fault.call, call.label) and fires(fault.when, numbers[i]):
            return Decision("fault", fault=i, n=numbers[i])
    if call.kind == "model":
        return Decision("real")  # model calls are what is being tested; never mocked
    if call.kind == "http":
        if call.method in READ_METHODS:
            return Decision("real")
        return Decision("refused", reason="model_host_write")
    for i, mock in enumerate(plan.mocks):
        if mock.call == call.label:
            return Decision("mock", mock=i)
    if matches_real(list(plan.real), call.label):
        return Decision("real")
    return Decision("refused", reason="unlisted")
