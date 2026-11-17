"""Judging: does an answer meet the rules a team wrote? Deterministic, with no model.

The runner calls this with a run's raw answer, never the redacted one kept in the record.
What comes back names rules only, so no answer text leaves this module.
Every result is an object, so that printing it and exiting on it read the same facts.
"""

import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from scrimmage_core.redact import redact

PASS, FAIL, NO_RESULT = "pass", "fail", "no result"
LEFT_OUT = "left out"
# A fault row beside a baseline that did not pass: its rate says little, so none is shown.
BASELINE_FAILED = "baseline failed"
VERDICTS = (PASS, FAIL, NO_RESULT, BASELINE_FAILED)  # a row's result
RUN_VERDICTS = (PASS, FAIL, LEFT_OUT)  # how one run counts
EMPTY = "empty answer"
NOT_FIRED = "the fault did not fire"
TEST_MISTAKE = "a mistake in the test"
REFUSED = "refused"
Z_95 = 1.96
FORMAT = "Cf"  # the Unicode category of characters that do not show
COMBINING_DOT = "\u0307"
BLANK = dict.fromkeys(map(ord, "\u034f\ufe0e\ufe0f\u1160"))  # blank, yet not of that category
# A crash or a time-out is a result about the agent. Every other ending with no answer
# says nothing about it.
ENDED_BADLY = ("crashed", "timed out")
KINDS = ("expect", "forbid", "empty", *ENDED_BADLY, REFUSED)  # what a broken rule can be
# Forms a model picks freely, so that a rule written the plain way still matches.
PLAIN_FORMS = str.maketrans(
    {
        **dict.fromkeys("\u2018\u2019", "'"),
        **dict.fromkeys("\u201c\u201d", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    }
)


@dataclass(frozen=True)
class Rules:
    """What one row is judged by."""

    expect: tuple[tuple[str, ...], ...]  # every item must match, on any one of its alternatives
    forbid: tuple[str, ...]
    pass_rate: float


@dataclass(frozen=True)
class Broken:
    """One rule a run broke. The text is safe to print and to record."""

    kind: str  # one of KINDS
    text: str


@dataclass(frozen=True)
class RunJudgement:
    """How a run counts: a pass, a fail with what it broke, or left out with the reason."""

    broken: tuple[Broken, ...] = ()
    left_out: str | None = None

    @property
    def verdict(self) -> str:
        if self.left_out is not None:
            return LEFT_OUT
        return FAIL if self.broken else PASS


@dataclass(frozen=True)
class RowResult:
    verdict: str
    passed: int
    judged: int
    floor: int  # the judged runs this row needs for any verdict


def normal(text: str) -> str:
    """Text as it is compared: one form for each character, no case, single spaces.

    Format characters such as a zero-width space leave no mark, so they cannot hide a word.
    Casefold turns a dotted capital I into i and a dot of its own, which goes too.
    """
    plain = unicodedata.normalize("NFKC", text).translate(PLAIN_FORMS)
    shown = "".join(char for char in plain.translate(BLANK) if unicodedata.category(char) != FORMAT)
    return " ".join(shown.casefold().replace(COMBINING_DOT, "").split())


def quoted(item: tuple[str, ...]) -> str:
    return " or ".join(f'"{text}"' for text in item)


def broken_rules(answer: str, rules: Rules) -> tuple[Broken, ...]:
    """The rules this answer breaks. Empty is a pass."""
    text = normal(answer)
    if not text:
        return (Broken("empty", EMPTY),)
    # A team may forbid a secret by its value, so a rule can hold what a record must not.
    expect = [
        Broken("expect", redact(f"expect {quoted(item)} not found"))
        for item in rules.expect
        if not any(normal(alternative) in text for alternative in item)
    ]
    forbid = [
        Broken("forbid", redact(f'forbid "{word}" found'))
        for word in rules.forbid
        if normal(word) in text
    ]
    return (*expect, *forbid)


def judge_run(
    outcome: str,
    answer: str | None,
    exercised: bool,
    rules: Rules,
    mistake: bool = False,
    refused_call: str | None = None,
) -> RunJudgement:
    """How one run counts. refused_call names a call the agent chose under a fault."""
    if not exercised:
        return RunJudgement(left_out=NOT_FIRED)
    if refused_call is not None:
        return RunJudgement((Broken(REFUSED, f"refused {refused_call}"),))
    if mistake and outcome == "crashed":  # the test broke the run, so it shows nothing
        return RunJudgement(left_out=TEST_MISTAKE)
    if outcome in ENDED_BADLY:
        return RunJudgement((Broken(outcome, outcome),))
    if outcome != "answered":
        return RunJudgement(left_out=outcome)
    return RunJudgement(broken_rules(answer or "", rules))


def floor_runs(runs: int) -> int:
    """More than half the planned runs. Fewer judged runs mean the test did not test."""
    return runs // 2 + 1


def judge_row(
    runs_judged: Sequence[RunJudgement], runs: int, pass_rate: float, refused: bool
) -> RowResult:
    """A row's verdict: enough judged runs first, then the rate over those runs."""
    judged = [run for run in runs_judged if run.left_out is None]
    passed = sum(1 for run in judged if not run.broken)
    floor = floor_runs(runs)
    if any(broken.kind == REFUSED for run in judged for broken in run.broken):
        verdict = FAIL  # what the agent did under the fault, at any rate
    elif len(judged) < floor:
        verdict = NO_RESULT
    elif passed / len(judged) < pass_rate:
        verdict = FAIL
    else:
        verdict = NO_RESULT if refused else PASS
    return RowResult(verdict, passed, len(judged), floor)


def wilson(passed: int, judged: int) -> tuple[float, float]:
    """The 95% Wilson interval of a pass rate. It stays sensible at 0 of n and n of n."""
    z2 = Z_95 * Z_95
    rate = passed / judged
    scale = 1 + z2 / judged
    centre = (rate + z2 / (2 * judged)) / scale
    half = Z_95 * math.sqrt(rate * (1 - rate) / judged + z2 / (4 * judged * judged)) / scale
    return max(0.0, centre - half), min(1.0, centre + half)


def impossible(rules: Rules) -> str | None:
    """Every alternative of an expect item holds a forbid text: no answer can pass."""
    for item in rules.expect:
        if all(any(normal(w) in normal(a) for w in rules.forbid) for a in item):
            return redact(f"forbid {quoted(rules.forbid)} rules out expect {quoted(item)}")
    return None
