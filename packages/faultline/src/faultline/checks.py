"""Checks: how the agent reacted to its faults, read from the run. No model involved.

Each check is a plain function `(run, faults, config) -> findings`, registered in
CHECKS with its name and certainty. A check that needs a fault returns one finding
per fault it judged, flagged when the bad reaction happened. A check that needs no
fault carries the run's first fault, or None on a run without one.
Evidence is one line a person can verify in the trace.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from faultline.config import ChecksConfig
from faultline.model import Certainty, Fault, Finding, Run, ToolCall
from faultline.runs import cut

EVIDENCE_LIMIT = 80

Judge = Callable[[Run, tuple[Fault, ...], ChecksConfig], tuple[Finding, ...]]
# What one finding of a check is about, so a report can group them:
# one fault, one run that had a fault, or any run.
Scope = Literal["fault", "faulted_run", "run"]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    certainty: Certainty
    judge: Judge
    scope: Scope

    def finding(self, fault: Fault | None, flagged: bool, evidence: str) -> Finding:
        return Finding(fault, self.name, self.certainty, flagged, evidence)


def first(faults: tuple[Fault, ...]) -> Fault | None:
    return faults[0] if faults else None


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


def calls_after(run: Run, step: int | None) -> list[ToolCall]:
    """Tool calls asked for in a step after `step`. None means before any step.
    Calls sent in the same model reply are siblings, not later calls."""
    after = 0 if step is None else step
    return [c for s in run.steps if s.number > after for c in s.tool_calls]


def no_retry(
    run: Run, faults: tuple[Fault, ...], config: ChecksConfig
) -> tuple[Finding, ...]:
    """A failed tool call never made again in a later step with the same tool and the
    same arguments, or a failed model call with no step after it. A fault this check
    cannot place in the run gets no finding; the report counts it as not judged."""
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
        later = [c for c in calls_after(run, fault.step) if c.name == call.name]
        same = sum(same_arguments(c.arguments) == key for c in later)
        other = len(later) - same
        args = cut(call.arguments, EVIDENCE_LIMIT) or "no arguments"
        if same:
            evidence = (
                f"{call.name}({args}) retried {same} time(s) with the same arguments"
            )
        else:
            evidence = f"{call.name}({args}) never retried with the same arguments"
        if other:
            evidence += f"; called again {other} time(s) with different arguments"
        elif not same:
            evidence += "; never called again"
        findings.append(NO_RETRY.finding(fault, not same, evidence))
    return tuple(findings)


NO_RETRY = Check("no_retry", Certainty.CERTAIN, no_retry, "fault")


# Names a system gives: lowercase words joined by "-", "_" or ".", at least one joiner.
# Not inside a longer token, and a sentence's final period is not a joiner.
NAME = re.compile(r"(?<![\w./-])[a-z0-9]+(?:[-_.][a-z0-9]+)+(?![\w-]|\.\w)")
IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w-]|\.\w)")
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
HEX_ID = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
CODE = re.compile(r"`[^`\n]+`")  # a Markdown code span
LETTER = re.compile(r"[a-z]", re.IGNORECASE)
DIGIT = re.compile(r"\d")
NUMBER_FIRST = re.compile(r"\d+[-_.]")  # 15-minute, 8.1s: a number with a unit
WORDS_BY_HYPHEN = re.compile(r"[a-z]+(?:-[a-z]+)+")  # gold-tier, but also cart-api


def identifiers(text: str) -> set[str]:
    """The identifiers in `text`, lowercased: system names, IPv4 addresses, UUIDs and
    hex ids of 8 or more characters. Never a plain number or a number with a unit.

    Words joined only by hyphens read the same as English compounds (gold-tier,
    crash-looping), so they count only inside a code span, where a writer marks a name.
    """
    code = [match.span() for match in CODE.finditer(text)]

    def in_code(start: int) -> bool:
        return any(begin < start < end for begin, end in code)

    found = set()
    for match in NAME.finditer(text):
        token = match.group()
        if not LETTER.search(token) or NUMBER_FIRST.match(token):
            continue
        if WORDS_BY_HYPHEN.fullmatch(token) and not in_code(match.start()):
            continue
        found.add(token)
    found |= set(IPV4.findall(text))
    found |= {uuid.lower() for uuid in UUID.findall(text)}
    found |= {
        hex_id.lower()
        for hex_id in HEX_ID.findall(text)
        if DIGIT.search(hex_id) and LETTER.search(hex_id)
    }
    return found


def unsupported_value(
    run: Run, faults: tuple[Fault, ...], config: ChecksConfig
) -> tuple[Finding, ...]:
    """The answer names an identifier found in no tool result, tool argument, tool
    name, nor the question. Without recorded content every result reads as blank, so
    such a run is not judged."""
    if not run.answer or not run.content_recorded:
        return ()
    seen = [run.question or ""]
    seen += [f"{c.name}\n{c.arguments}\n{c.result}" for c in run.tool_calls]
    haystack = "\n".join(seen).lower()
    named = identifiers(run.answer)
    missing = sorted(name for name in named if name not in haystack)
    if missing:
        evidence = cut(
            "answer names " + ", ".join(missing) + ", found in no tool result",
            EVIDENCE_LIMIT,
        )
    elif named:
        evidence = f"all {len(named)} identifier(s) in the answer are in the trace"
    else:
        evidence = "the answer names no identifier"
    return (UNSUPPORTED_VALUE.finding(first(faults), bool(missing), evidence),)


UNSUPPORTED_VALUE = Check(
    "unsupported_value", Certainty.HEURISTIC, unsupported_value, "run"
)


def repeated_call(
    run: Run, faults: tuple[Fault, ...], config: ChecksConfig
) -> tuple[Finding, ...]:
    """The same tool called with the same arguments more than the limit allows. One
    finding per (tool, arguments) pair over the limit. Without recorded content every
    call reads as having no arguments, so such a run is not judged."""
    if not run.content_recorded:
        return ()
    counts: dict[tuple[str, str], list[ToolCall]] = {}
    for call in sorted(run.tool_calls, key=lambda c: c.started):
        counts.setdefault((call.name, same_arguments(call.arguments)), []).append(call)
    limit = config.repeated_call_limit
    findings = []
    for (name, _), made in counts.items():
        if len(made) > limit:
            args = cut(made[0].arguments, EVIDENCE_LIMIT) or "no arguments"
            evidence = f"{name}({args}) called {len(made)} times, limit {limit}"
            findings.append(REPEATED_CALL.finding(first(faults), True, evidence))
    return tuple(findings)


REPEATED_CALL = Check("repeated_call", Certainty.CERTAIN, repeated_call, "run")


DISCLOSURE_WORDS = (
    "error", "errored", "fail", "failed", "failure", "unable", "could not",
    "couldn't", "cannot", "can't", "timeout", "timed out", "unavailable",
    "unreachable", "empty", "no data", "missing", "uncertain", "unclear", "not sure",
    "unknown", "may", "might", "possibly", "likely", "appears", "seems",
)  # fmt: skip
DISCLOSURE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in DISCLOSURE_WORDS) + r")\b",
    re.IGNORECASE,
)


def not_disclosed(
    run: Run, faults: tuple[Fault, ...], config: ChecksConfig
) -> tuple[Finding, ...]:
    """A run with a fault whose answer mentions neither a failure nor any uncertainty.
    One finding per run, carrying its first fault. A run without an answer is not
    judged: there is nothing to read."""
    if not faults or not run.answer:
        return ()
    # A typographic apostrophe, as in couldn’t, reads as a plain one.
    said = DISCLOSURE.search(run.answer.replace("’", "'"))
    if said:
        evidence = f'the answer says "{said.group()}"'
    else:
        evidence = (
            f"the answer mentions no failure or doubt after {len(faults)} fault(s)"
        )
    return (NOT_DISCLOSED.finding(faults[0], said is None, evidence),)


NOT_DISCLOSED = Check(
    "not_disclosed", Certainty.HEURISTIC, not_disclosed, "faulted_run"
)

CHECKS: tuple[Check, ...] = (NO_RETRY, UNSUPPORTED_VALUE, REPEATED_CALL, NOT_DISCLOSED)
