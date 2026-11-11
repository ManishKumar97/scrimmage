"""Suggestions for a failing run: rules over its record, with no model and no write.

Every suggestion is a heuristic and says so. It has a stable code, a text that starts with
the label, and evidence: the seq and proc of the record lines it read. A rule fires only on
evidence in those lines. The text says "after", never "because": it cannot show a cause.

Standard library only apart from the judge's words, so `show` can use it without the runner.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from scrimmage_core import judge
from scrimmage_core.redact import redact

Doc = dict[str, object]
LABEL = "suggestion (a heuristic):"
RETRIES = 3  # calls to the faulted call, after its fault line, that make a retry loop
SHOWN_EVIDENCE = 10  # evidence lines named for one suggestion
CUT = 80  # characters of a call label or a mistake kept in a text
# An outcome that says nothing about the agent: the same ones the judge leaves out.
NOT_AGENT = ("budget reached", "not reserved", "infrastructure error", "stopped")
REFUSED_CALL = "refused call"
CRASHED, TIMED_OUT = judge.ENDED_BADLY

# The main codes in the order the rules are checked, then the secondary ones.
MAIN_CODES = (
    "test_mistake",
    "not_agent",
    "fault_not_fired",
    "refused_call",
    "refused_on_baseline",
    "crash_after_fault",
    "crash_on_baseline",
    "retry_loop",
    "timeout_after_fault",
    "timeout_on_baseline",
    "forbid_after_fault",
    "expect_after_fault",
    "empty_answer",
    "rule_on_baseline",
)
EXTRA_CODES = ("mock_miss_real", "mock_miss_refused")
CODES = (*MAIN_CODES, *EXTRA_CODES)


@dataclass(frozen=True)
class Suggestion:
    code: str
    text: str
    evidence: tuple[tuple[int, object], ...]  # (seq, proc) of each line read

    @property
    def doc(self) -> Doc:
        return {
            "code": self.code,
            "text": self.text,
            "heuristic": True,
            "evidence": [{"seq": seq, "proc": proc} for seq, proc in self.evidence],
        }


@dataclass(frozen=True)
class NoRule:
    """No suggestion for this run, and the reason, which is never shown as advice."""

    reason: str


def seq_of(line: Doc) -> int:
    value = line.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cite(*lines: Doc) -> tuple[tuple[int, object], ...]:
    return tuple((seq_of(x), x.get("proc")) for x in lines[:SHOWN_EVIDENCE])


def said(value: object) -> str:
    """A label or a message from a record, redacted again and cut."""
    text = " ".join(redact(str(value)).split())
    return text if len(text) <= CUT else text[: CUT - 3] + "..."


def make(code: str, text: str, *lines: Doc) -> Suggestion:
    return Suggestion(code, f"{LABEL} {text}", cite(*lines))


def with_action(lines: Sequence[Doc], action: str) -> list[Doc]:
    return [x for x in lines if x.get("action") == action]


def fault_of(run: Doc, row: Doc | None) -> bool:
    """A fault row, as the row says; with no row, any row not named baseline."""
    if row is not None:
        return row.get("fault") is not None
    return run.get("row") != "baseline"


def kinds_of(run: Doc) -> set[str]:
    broken = run.get("broken")
    if not isinstance(broken, list):
        return set()
    return {
        str(cast(Doc, b).get("kind")) for b in cast("list[object]", broken) if isinstance(b, dict)
    }


def injected(fault_line: Doc) -> str:
    fault = fault_line.get("fault")
    do = cast(Doc, fault).get("do") if isinstance(fault, dict) else None
    return f"injected {said(do)}" if isinstance(do, str) else "injected failure"


def calls_after(lines: Sequence[Doc], fault_line: Doc) -> list[Doc]:
    """Calls to the faulted call after its fault line, in the same process.

    A sequence number counts the lines of one process, so the order is only read there.
    """
    return [
        x
        for x in lines
        if x.get("call") == fault_line.get("call")
        and x.get("proc") == fault_line.get("proc")
        and x is not fault_line
        and seq_of(x) > seq_of(fault_line)
    ]


def main(run: Doc, lines: Sequence[Doc], row: Doc | None) -> Suggestion | NoRule:
    """The one main suggestion for a run, or why there is none. Never raises."""
    verdict, outcome, left_out = run.get("judged"), run.get("outcome"), run.get("left_out")
    if verdict == judge.PASS:
        return NoRule("passed: a pass gets no suggestion")
    if verdict not in (judge.FAIL, judge.LEFT_OUT) or not isinstance(outcome, str):
        return NoRule("the run did not end, so it has no verdict")
    kinds = kinds_of(run)
    fault = fault_of(run, row)
    last = list(lines[-1:])
    fault_lines = with_action(lines, "fault")
    refused = with_action(lines, "refused")
    x = fault_lines[0] if fault_lines else None
    context = [x] if x is not None else []  # a rule about the whole run still cites the fault
    name = said(x.get("call")) if x is not None else ""

    if left_out == judge.TEST_MISTAKE:  # first, so a crash never reads as the fault's doing
        found = [m for m in lines if "mistake" in m]
        if not found:
            return NoRule("the test's mistake is not among the lines read")
        more = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
        return make(
            "test_mistake",
            f"the test itself has a mistake: {said(found[0]['mistake'])}{more}. "
            "Fix the test before reading this run.",
            *found,
        )
    if outcome in NOT_AGENT:
        return make(
            "not_agent",
            f'this run ended with "{outcome}", which says nothing about the agent. '
            "There is nothing to fix in the agent for this run; the test's notes give the reason.",
            *last,
        )
    if fault and left_out == judge.NOT_FIRED:
        return make(
            "fault_not_fired",
            "the fault did not fire: the agent never called the faulted call, so this row "
            "tested nothing. Check the fault's label and the test's question.",
            *last,
        )
    if fault and judge.REFUSED in kinds and refused:
        return make(
            "refused_call",
            f"under this fault the agent called {said(refused[0].get('call'))}, which the test "
            "does not allow. Mocking it would hide this; look at why the agent reaches for it "
            "when the fault fires.",
            *context,
            *refused,
        )
    if not fault and outcome == REFUSED_CALL and refused:
        return make(
            "refused_on_baseline",
            f"the agent made a call the test does not allow ({said(refused[0].get('call'))}). "
            "Add it to `real`, mock it, or check why the agent made it.",
            *refused,
        )
    if outcome == CRASHED:
        if fault and x is not None:
            return make(
                "crash_after_fault",
                f"the agent crashed after an injected failure in {name} ({injected(x)}). "
                f"Scrimmage cannot see how the two are linked; catch the error from {name} "
                "and run the test again.",
                x,
            )
        if not fault:
            return make(
                "crash_on_baseline",
                "the agent crashed with no injected failure; the last line of its log is shown "
                "with the run. Fix the baseline before reading the fault rows.",
                *last,
            )
    if outcome == TIMED_OUT:
        if fault and x is not None:
            later = calls_after(lines, x)
            if len(later) >= RETRIES:
                return make(
                    "retry_loop",
                    f"the agent called {name} {len(later)} times after it failed ({injected(x)}), "
                    f"and the run then timed out. Cap the retries of {name}.",
                    x,
                    *later,
                )
            return make(
                "timeout_after_fault",
                f"the run ran out of time after an injected failure in {name}. An injected "
                f"failure comes back at once, so the time went after it; handle the error from "
                f"{name} so the agent answers instead of going on.",
                x,
                *later,
            )
        if not fault:
            return make(
                "timeout_on_baseline",
                "the run ran out of time with no injected failure. Check `run_timeout` and "
                "the baseline before reading the fault rows.",
                *last,
            )
    if fault and x is not None and "forbid" in kinds:
        return make(
            "forbid_after_fault",
            f"the answer holds text the test forbids, after an injected failure in {name}. "
            "Scrimmage cannot see where that text came from; look at what your code gives the "
            f"model when {name} fails.",
            x,
        )
    if fault and x is not None and "expect" in kinds:
        return make(
            "expect_after_fault",
            f"the answer lacks the expected text, after an injected failure in {name}. Look at "
            f"what the agent tells the user when {name} fails.",
            x,
        )
    if "empty" in kinds:
        return make(
            "empty_answer",
            "the agent gave no answer. Give a fallback answer when a step fails.",
            *context,
            *last,
        )
    if not fault and kinds & {"expect", "forbid"}:
        return make(
            "rule_on_baseline",
            "the baseline fails the test's rules. Fix it before reading the fault rows.",
            *last,
        )
    return NoRule("no rule fits this run")


def extras(run: Doc, lines: Sequence[Doc]) -> list[Suggestion]:
    """At most one secondary suggestion: the first mock miss, by what the gate did with it."""
    if run.get("judged") == judge.PASS:
        return []
    for line in lines:
        if "mock_miss" not in line:
            continue
        call = said(line.get("call"))
        if line.get("action") == "real":
            return [
                make(
                    "mock_miss_real",
                    f"a mock on {call} matched no call, so it ran for real. Record the varying "
                    "arguments with `record --runs`.",
                    line,
                )
            ]
        if line.get("action") == "refused":
            return [
                make(
                    "mock_miss_refused",
                    f"a mock on {call} matched no call, and the call is not allowed to run for "
                    "real, so it was refused. Record the varying arguments with `record --runs`.",
                    line,
                )
            ]
    return []


def for_run(run: Doc, lines: Sequence[Doc], row: Doc | None) -> list[Suggestion]:
    """The main suggestion, if any, then the secondary one."""
    chosen = main(run, lines, row)
    return [*([chosen] if isinstance(chosen, Suggestion) else []), *extras(run, lines)]
