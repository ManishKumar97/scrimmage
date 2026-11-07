"""Judging: does an answer meet the rules a team wrote? Deterministic, with no model.

The runner calls this with a run's raw answer, never the redacted one kept in the record.
What comes back names rules only, so no answer text leaves this module.
Every result is an object, so that printing it and exiting on it read the same facts.
"""

import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from derail_sdk.redact import redact

PASS, FAIL, NO_RESULT = "pass", "fail", "no result"
LEFT_OUT = "left out"
EMPTY = "empty answer"
NOT_FIRED = "the fault did not fire"
Z_95 = 1.96
# A crash or a time-out is a result about the agent. Every other ending with no answer
# says nothing about it.
ENDED_BADLY = ("crashed", "timed out")
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

    kind: str  # expect, forbid, empty, crashed or timed out
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
    needed: int  # the judged runs this row needs for a pass


def normal(text: str) -> str:
    """Text as it is compared: one form for each character, no case, single spaces."""
    plain = unicodedata.normalize("NFKC", text).translate(PLAIN_FORMS)
    return " ".join(plain.casefold().split())


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


def judge_run(outcome: str, answer: str | None, exercised: bool, rules: Rules) -> RunJudgement:
    if not exercised:
        return RunJudgement(left_out=NOT_FIRED)
    if outcome in ENDED_BADLY:
        return RunJudgement((Broken(outcome, outcome),))
    if outcome != "answered":
        return RunJudgement(left_out=outcome)
    return RunJudgement(broken_rules(answer or "", rules))


def needed_runs(pass_rate: float, runs: int) -> int:
    """The fewest of `runs` that reach the rate. The same sum as the verdict's, so a share
    such as 0.07 of 100 does not land on 8 from a rounding error in a product."""
    return next(n for n in range(1, runs + 1) if n / runs >= pass_rate)


def judge_row(
    runs_judged: Sequence[RunJudgement], runs: int, pass_rate: float, refused: bool
) -> RowResult:
    """A row's verdict. A rate below the line fails at once; a pass needs enough runs."""
    judged = [run for run in runs_judged if run.left_out is None]
    passed = sum(1 for run in judged if not run.broken)
    needed = needed_runs(pass_rate, runs)
    if not judged:
        verdict = NO_RESULT
    elif passed / len(judged) < pass_rate:
        verdict = FAIL
    else:
        verdict = NO_RESULT if refused or len(judged) < needed else PASS
    return RowResult(verdict, passed, len(judged), needed)


def wilson(passed: int, judged: int) -> tuple[float, float]:
    """The 95% Wilson interval of a pass rate. It stays sensible at 0 of n and n of n."""
    z2 = Z_95 * Z_95
    rate = passed / judged
    scale = 1 + z2 / judged
    centre = (rate + z2 / (2 * judged)) / scale
    half = Z_95 * math.sqrt(rate * (1 - rate) / judged + z2 / (4 * judged * judged)) / scale
    return max(0.0, centre - half), min(1.0, centre + half)


def impossible(rules: Rules) -> str | None:
    """A forbid text inside every alternative of an expect item: no answer can pass."""
    for item in rules.expect:
        for word in rules.forbid:
            if all(normal(word) in normal(alternative) for alternative in item):
                return f'forbid "{word}" rules out expect {quoted(item)}'
    return None
