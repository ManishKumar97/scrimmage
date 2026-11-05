"""The decision engine: for one call in one run, fault, mock, real or refused.

A pure function. The gate counts the call first, then asks. Faults win over mocks,
because the baseline row uses the mocks alone and each fault row adds one fault.
"""

from dataclasses import dataclass
from typing import Literal

from faultline_sdk.labels import matches_real
from faultline_sdk.plan import Plan

Action = Literal["fault", "mock", "real", "refused"]


@dataclass(frozen=True)
class Call:
    """What a gate saw. In this version every call is a tool call."""

    kind: Literal["tool"]
    label: str
    name: str


@dataclass(frozen=True)
class Decision:
    action: Action
    fault: int | None = None  # index into plan.faults
    mock: int | None = None  # index into plan.mocks
    n: int = 0  # the fault's count of matching calls, this one included


def count(call: Call, plan: Plan, counts: list[int]) -> None:
    """Add this call to the counter of every fault that names it exactly."""
    for i, fault in enumerate(plan.faults):
        if fault.call == call.label:
            counts[i] += 1


def decide(call: Call, plan: Plan, counts: list[int]) -> Decision:
    for i, fault in enumerate(plan.faults):
        if fault.call == call.label:  # "always" is the only schedule, so it fires
            return Decision("fault", fault=i, n=counts[i])
    for i, mock in enumerate(plan.mocks):
        if mock.call == call.label:
            return Decision("mock", mock=i)
    if matches_real(list(plan.real), call.label):
        return Decision("real")
    return Decision("refused")
