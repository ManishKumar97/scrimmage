"""Checks: how the agent reacted to its faults, read from the run. No model involved.

Each check is a plain function `(run, faults, config) -> findings`, registered in
CHECKS with its name and certainty. A check that needs a fault returns one finding
per fault it judged, flagged when the bad reaction happened. Evidence is one line a
person can verify in the trace.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

from faultline.config import ChecksConfig
from faultline.model import Certainty, Fault, Finding, Run, ToolCall
from faultline.runs import cut

EVIDENCE_LIMIT = 80

Judge = Callable[[Run, tuple[Fault, ...], ChecksConfig], tuple[Finding, ...]]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    certainty: Certainty
    judge: Judge

    def finding(self, fault: Fault, flagged: bool, evidence: str) -> Finding:
        return Finding(fault, self.name, self.certainty, flagged, evidence)


def same_arguments(arguments: str) -> str:
    """A key equal for two calls with the same arguments: JSON compared by value,
    so key order and spacing do not matter, anything else compared as written."""
    try:
        return json.dumps(json.loads(arguments), sort_keys=True)
    except ValueError:
        return arguments.strip()


def faulted_call(run: Run, fault: Fault) -> ToolCall | None:
    """The tool call a tool fault happened on."""
    step = next((s for s in run.steps if s.number == fault.step), None)
    calls = run.unplaced if step is None else step.tool_calls
    return next(
        (c for c in calls if c.call_id == fault.call_id and c.name == fault.tool),
        None,
    )


def no_retry(
    run: Run, faults: tuple[Fault, ...], config: ChecksConfig
) -> tuple[Finding, ...]:
    """A failed tool call never made again with the same arguments, or a failed model
    call with no step after it."""
    findings = []
    last_step = max((s.number for s in run.steps), default=None)
    for fault in faults:
        if fault.tool is None:  # a model fault
            if fault.step is None:
                continue
            flagged = fault.step == last_step
            evidence = (
                f"model call at step {fault.step} was the last step"
                if flagged
                else f"model call at step {fault.step}, the run went on to step {last_step}"
            )
            findings.append(NO_RETRY.finding(fault, flagged, evidence))
            continue
        call = faulted_call(run, fault)
        if call is None:
            continue
        key = same_arguments(call.arguments)
        again = [
            c
            for c in run.tool_calls
            if c.started > call.started
            and c.name == call.name
            and same_arguments(c.arguments) == key
        ]
        args = cut(call.arguments, EVIDENCE_LIMIT) or "no arguments"
        if again:
            evidence = f"{call.name}({args}) called again {len(again)} time(s)"
        else:
            evidence = f"{call.name}({args}) never called again with these arguments"
        findings.append(NO_RETRY.finding(fault, not again, evidence))
    return tuple(findings)


NO_RETRY = Check("no_retry", Certainty.CERTAIN, no_retry)

CHECKS: tuple[Check, ...] = (NO_RETRY,)
