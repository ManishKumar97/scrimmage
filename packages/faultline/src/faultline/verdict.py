"""The verdict on one run: passed, failed loudly or failed silently.

Rules, in order, with no model or person judging the run:

1. A non-zero exit, a run or step error at the end, or no answer: failed loudly.
   So is a run that stopped at a limit: its last reply was cut at the token limit,
   or the model was still asking for a tool when the run ended (a step, token or
   time limit of the agent's own).
2. The reference: the team's expected answer, else the answer of the task's first
   clean run that got past rule 1.
3. Compare. Two answers that both parse as JSON compare exactly. Free text compares
   the identifiers (the same extraction as the unsupported_value check) and the
   numbers: the answer must hold each one the reference holds. A reference with
   neither compares its words. A team can plug in its own matcher instead.
4. The same: passed. Different, and the answer says something failed or is in
   doubt (the not_disclosed check's words): passed, the agent told the truth.
   Different, and the answer says nothing: failed silently.

Failed silently says the answer is wrong. That is said only when we injected a
fault in the run or the team gave the expected answer. A clean run that differs
from another clean run has no verdict: two runs disagree, and neither is known right.

A run with no reference past rule 1 has no verdict. Nor has a run whose last reply
was a stream the proxy could not read: its answer is not in the trace, which is no
fault of the agent's.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from faultline.checks import disclosure, identifiers, tokens
from faultline.faults import Fault, ModelFault, ToolFault
from faultline.model import Run, RunVerdict, Verdict

NO_REFERENCE = "no reference: pass --expect or use faultline test"
STREAMED = "no verdict: answer not recorded (streamed reply)"
# A finish reason of the last step that says the run stopped at a limit.
TOKEN_LIMIT = "length"
TOOL_CALL = "tool_call"
# A number as a whole token, with an optional unit (3 restarts, 8.1s, 50%, 1,000).
# Never part of a name, an address, a version, a date or a time.
NUMBER = re.compile(
    r"(?<![\w.:/-])\d+(?:,\d{3})*(?:\.\d+)?(?![\w-]*[-_.:/]\w)(?=[a-z%]*(?:\W|$))",
    re.IGNORECASE,
)
SHOWN = 5  # how many missing items a reason names

# A team's own comparison: (answer, reference), true when they say the same.
Matcher = Callable[[str, str], object]


@dataclass(frozen=True)
class Reference:
    """What a run's answer is compared with."""

    text: str
    expected: bool  # the team gave it. Else it is a clean run's answer
    source: str  # where it came from, for the reason: "the expected answer"


def expected(text: str) -> Reference:
    return Reference(text, True, "the expected answer")


def injected(run: Run | None) -> list[str]:
    """The fault specs the proxy applied in the run, in the order they were applied."""
    if run is None:
        return []
    found = [step.injected for step in run.steps]
    found += [call.injected for call in run.tool_calls]
    return list(dict.fromkeys(spec for spec in found if spec))


def loud(run: Run | None, exit_code: int) -> str | None:
    """Why rule 1 fails the run loudly, or None when it gets past rule 1.

    A missing answer is left to the caller, which may know it was not recorded.
    """
    if exit_code:
        return f"exit code {exit_code}"
    if run is None:
        return "no model call recorded"
    if run.error_type:
        return f"error {run.error_type}"
    if not run.steps:
        return "no model call recorded"
    last = run.steps[-1]
    if last.error_type:
        return f"the last model call failed: {last.error_type}"
    if last.finish_reason == TOKEN_LIMIT:
        return "the last reply stopped at the token limit"
    if last.finish_reason == TOOL_CALL or last.tool_calls:
        return "the run ended while the model asked for a tool: a step, token or time limit"
    return None


def numbers(text: str) -> dict[Decimal, str]:
    """The numbers in `text`, by value, each with its first spelling."""
    found: dict[Decimal, str] = {}
    for match in NUMBER.finditer(text):
        try:
            found.setdefault(Decimal(match.group().replace(",", "")), match.group())
        except InvalidOperation:
            continue
    return found


def as_json(text: str) -> tuple[bool, object]:
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


def lacking(answer: str, reference: str) -> list[str]:
    """What the reference holds that the answer does not, sorted.

    The identifiers and numbers of the reference. A reference with neither, such as
    an expected answer of one service name, gives its words.
    An identifier counts when the answer has it as a whole token, as the
    unsupported_value check counts one against the trace.
    """
    wanted = numbers(reference)
    have = numbers(answer)
    missing = [spelled for value, spelled in wanted.items() if value not in have]
    named = identifiers(reference)
    if not named and not wanted:
        named = tokens(reference)
    missing += sorted(named - tokens(answer))
    return sorted(missing)


def compare(answer: str, reference: str, matcher: Matcher | None) -> tuple[bool, str]:
    """Whether the answer says what the reference says, and how it differs if not."""
    if matcher is not None:
        return bool(matcher(answer, reference)), "differs by the team's matcher"
    answer_parsed, answer_value = as_json(answer)
    reference_parsed, reference_value = as_json(reference)
    if answer_parsed and reference_parsed:
        return answer_value == reference_value, "is different JSON"
    missing = lacking(answer, reference)
    shown = ", ".join(f'"{item}"' for item in missing[:SHOWN])
    more = f" and {len(missing) - SHOWN} more" if len(missing) > SHOWN else ""
    return not missing, f"lacks {shown}{more}"


def judge(
    run: Run | None,
    trace_id: str,
    exit_code: int,
    reference: Reference | str | None,
    answer_unrecorded: bool = False,
    matcher: Matcher | None = None,
) -> RunVerdict | str:
    """The verdict, or why there is none.

    `reference` is a plain string for an expected answer the team gave.
    `answer_unrecorded` says the last reply was a stream the proxy could not read,
    so a missing answer is the proxy's gap, not the agent's failure.
    """
    if isinstance(reference, str):
        reference = expected(reference)
    faults = injected(run)

    def verdict(kind: Verdict, reason: str) -> RunVerdict:
        if faults:
            reason += f"; injected {', '.join(faults)}"
        return RunVerdict(trace_id, kind, reason, ", ".join(faults) or None)

    why = loud(run, exit_code)
    if why:
        return verdict(Verdict.FAILED_LOUDLY, why)
    assert run is not None  # rule 1 fails a run that is not there
    if not run.answer and answer_unrecorded:
        return STREAMED
    if not run.answer:
        return verdict(Verdict.FAILED_LOUDLY, "no answer")
    if reference is None:
        return NO_REFERENCE
    try:
        same, difference = compare(run.answer, reference.text, matcher)
    except Exception as exc:  # noqa: BLE001 - the matcher is the team's code
        return f"no verdict: the matcher raised {type(exc).__name__}: {exc}"
    if same:
        return verdict(Verdict.PASSED, f"answer matches {reference.source}")
    against = f"answer {difference}, compared with {reference.source}"
    said = disclosure(run.answer)
    if said:
        return verdict(Verdict.PASSED, f'{against}, but says "{said}"')
    if not faults and not reference.expected:
        return (
            f"no verdict: {against}, with no fault injected and no expected answer, "
            "so neither answer is known right"
        )
    return verdict(Verdict.FAILED_SILENTLY, against)


def never_applied(
    faults: Sequence[Fault], run: Run | None, wrapped: bool
) -> list[tuple[str, str]]:
    """Each requested fault the trace shows was never applied, with the likely reason.

    `wrapped` says an MCP server ran under faultline mcp-wrap in this run.
    """
    applied = set(injected(run))
    missed = []
    for fault in faults:
        if fault.spec in applied:
            continue
        missed.append((fault.spec, why_not(fault, run, wrapped)))
    return missed


def why_not(fault: Fault, run: Run | None, wrapped: bool) -> str:
    if isinstance(fault, ToolFault):
        if not wrapped:
            return "no MCP server ran under faultline mcp-wrap"
        called = sorted({call.name for call in run.tool_calls}) if run else []
        if fault.tool not in called:
            seen = ", ".join(called) or "none"
            return f"the agent never called {fault.tool}; MCP tools called: {seen}"
        assert run  # the tool was called
        times = sum(call.name == fault.tool for call in run.tool_calls)
        if fault.call is not None and times < fault.call:
            return (
                f"the agent called {fault.tool} {times} time(s), "
                f"so there was no call {fault.call}"
            )
        return "the tool was called, but no call was answered by the fault"
    assert isinstance(fault, ModelFault)
    steps = len(run.steps) if run else 0
    if steps < fault.step:
        return f"the run made {steps} model call(s), so there was no step {fault.step}"
    return "the step ran, but no call was answered by the fault"


def not_applied_line(specs: Sequence[str]) -> str:
    return f"verdict: not_applied (never applied: {', '.join(specs)})"


def line(verdict: RunVerdict | str) -> str:
    """The one line `faultline run` prints after the agent exits."""
    if isinstance(verdict, str):
        return verdict
    return f"verdict: {verdict.verdict.value} ({verdict.reason})"
