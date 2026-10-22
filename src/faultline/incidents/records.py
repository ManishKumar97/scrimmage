"""One result line per trial, and the summary over those lines."""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

CORRECT = "correct"
WRONG = "wrong"
NO_VERDICT = "no verdict"  # the answer had no ROOT CAUSE SERVICE line
NO_ALERT = "no alert"  # no page came: a monitoring gap, not an agent error
SETUP_FAILED = "setup failed"  # the test bed never became healthy
AGENT_ERROR = "agent error"  # the run raised instead of ending with a stop reason


@dataclass
class Record:
    sha: str
    incident: str  # the catalog name, or the control's
    trial: int
    outcome: str
    started_at: str
    expected: str  # the correct service, or "none" for the control
    correct: bool = False
    alert: str | None = None
    alerts: list[str] = field(default_factory=list)
    question: str | None = None
    answer: str | None = None
    service: str | None = None  # the service the agent named
    stop_reason: str | None = None
    steps: int = 0
    tokens: int = 0
    cache_read_tokens: int = 0
    seconds: float = 0.0
    trace_file: str | None = None
    recovery: str = "not needed"  # "ok", or why the test bed did not come back
    detail: str = ""  # why a trial has no answer


_LABEL = re.compile(r"\bservice_name=([A-Za-z0-9_.-]+?)\.?(?:,|\s|$)")


def label_service(question: str | None) -> str | None:
    """The `service_name` label the page carried, read from the question the agent got.

    Read from the question so records written before this existed still count.
    """
    match = _LABEL.search(question or "")
    return match[1] if match else None


def append(path: Path, record: Record) -> None:
    """Add one line and put it on disk before the next trial starts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(asdict(record)) + "\n")
        out.flush()
        # An interrupted run resumes from this file, so a line must not be lost.
        os.fsync(out.fileno())


def load(path: Path) -> list[Record]:
    """All records in the file. A torn last line, from a crash, is dropped."""
    if not path.exists():
        return []
    records = []
    lines = path.read_text().splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            records.append(Record(**json.loads(line)))
        except (ValueError, TypeError):
            if number == len(lines):
                break
            raise ValueError(f"{path}:{number} is not a result line") from None
    return records


def latest(records: list[Record]) -> dict[tuple[str, int], Record]:
    """The newest record of each (incident, trial). A retry replaces its failed try."""
    return {(r.incident, r.trial): r for r in records}


def finished(records: list[Record]) -> set[tuple[str, int]]:
    """Trials a resumed run skips. A failed setup is tried again."""
    return {k for k, r in latest(records).items() if r.outcome != SETUP_FAILED}


@dataclass
class Tally:
    trials: int = 0
    correct: int = 0
    wrong: int = 0
    no_verdict: int = 0
    no_alert: int = 0
    setup_failed: int = 0
    agent_error: int = 0
    label_correct: int = 0  # pages whose service_name label is the answer key

    def add(self, record: Record) -> None:
        self.trials += 1
        field_name = record.outcome.replace(" ", "_")
        setattr(self, field_name, getattr(self, field_name) + 1)
        if record.outcome not in (NO_ALERT, SETUP_FAILED):
            self.label_correct += label_service(record.question) == record.expected

    @property
    def paged(self) -> int:
        """Trials that got an alert, so the agent was asked."""
        return self.trials - self.no_alert - self.setup_failed

    def accuracy(self) -> str:
        return f"{self.correct}/{self.paged}" if self.paged else "-"

    def label_baseline(self) -> str:
        """What answering the page's own service_name label would have scored."""
        return f"{self.label_correct}/{self.paged}" if self.paged else "-"


def summarize(records: list[Record], control: str) -> str:
    """Per-incident and overall accuracy, with the trials that never reached the agent.

    The label baseline is the score of an agent that answers the page's service_name
    label and does nothing else. Accuracy only means something above it.
    """
    per: dict[str, Tally] = {}
    for record in latest(records).values():
        per.setdefault(record.incident, Tally()).add(record)
    header = (
        f"{'incident':40} {'correct/paged':>13} {'label baseline':>14} {'wrong':>5} "
        f"{'no verdict':>10} {'no alert':>8} {'setup failed':>12} {'agent error':>11}"
    )
    rows = [header]
    overall = Tally()

    def row(label: str, tally: Tally) -> str:
        return (
            f"{label:40} {tally.accuracy():>13} {tally.label_baseline():>14} "
            f"{tally.wrong:>5} {tally.no_verdict:>10} {tally.no_alert:>8} "
            f"{tally.setup_failed:>12} {tally.agent_error:>11}"
        )

    for name, tally in sorted(per.items(), key=lambda kv: (kv[0] == control, kv[0])):
        label = f"{name} (control)" if name == control else name
        rows.append(row(label, tally))
        if name != control:
            for key in vars(tally):
                setattr(overall, key, getattr(overall, key) + getattr(tally, key))
    rows.append(row("overall (faults only)", overall))
    shas = sorted({r.sha for r in latest(records).values()})
    rows.append(f"git: {', '.join(shas)}")
    return "\n".join(rows)
