"""Read the agent's one-line verdict from the end of its answer, and score it."""

import re
from dataclasses import dataclass
from typing import Literal

NONE = "none"  # the verdict for "no fault found"

# The line may carry markdown the model adds: bullets, bold, backticks, quotes, a full stop.
_LINE = re.compile(r"^[\s>*_`#-]*ROOT CAUSE SERVICE\s*:(?P<rest>.*)$", re.IGNORECASE)
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_DECORATION = " \t*_`'\"<>."

Outcome = Literal["correct", "wrong", "no verdict"]


@dataclass(frozen=True)
class Verdict:
    service: str | None  # None: the answer has no usable verdict line
    outcome: Outcome


def parse_verdict(answer: str) -> str | None:
    """The service named by the last verdict line, or None.

    A line whose value is not a single service name, such as 'none of these',
    is not a verdict. The last line wins, so a model that corrects itself is read
    as its final word.
    """
    found: str | None = None
    for line in answer.splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        value = match["rest"].strip(_DECORATION).lower()
        found = value if _NAME.fullmatch(value) else None
    return found


def score(answer: str, correct: str | None) -> Verdict:
    """Compare the verdict with the answer key. `correct` None means no fault was injected.

    A missing verdict is scored wrong and kept apart as 'no verdict': the run gave
    no answer at all, which is a different failure from a wrong service.
    """
    service = parse_verdict(answer)
    if service is None:
        return Verdict(None, "no verdict")
    key = NONE if correct is None else correct.lower()
    return Verdict(service, "correct" if service == key else "wrong")
