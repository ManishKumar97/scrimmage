"""One result line per trial, and the summary over those lines."""

import json
import os
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

    def add(self, outcome: str) -> None:
        self.trials += 1
        field_name = outcome.replace(" ", "_")
        setattr(self, field_name, getattr(self, field_name) + 1)

    @property
    def paged(self) -> int:
        """Trials that got an alert, so the agent was asked."""
        return self.trials - self.no_alert - self.setup_failed

    def accuracy(self) -> str:
        return f"{self.correct}/{self.paged}" if self.paged else "-"


def summarize(records: list[Record], control: str) -> str:
    """Per-incident and overall accuracy, with the trials that never reached the agent."""
    per: dict[str, Tally] = {}
    for record in latest(records).values():
        per.setdefault(record.incident, Tally()).add(record.outcome)
    header = f"{'incident':40} {'correct/paged':>13} {'wrong':>5} {'no verdict':>10} {'no alert':>8} {'setup failed':>12} {'agent error':>11}"
    rows = [header]
    overall = Tally()
    for name, tally in sorted(per.items(), key=lambda kv: (kv[0] == control, kv[0])):
        label = f"{name} (control)" if name == control else name
        rows.append(
            f"{label:40} {tally.accuracy():>13} {tally.wrong:>5} {tally.no_verdict:>10} "
            f"{tally.no_alert:>8} {tally.setup_failed:>12} {tally.agent_error:>11}"
        )
        if name != control:
            for key in vars(tally):
                setattr(overall, key, getattr(overall, key) + getattr(tally, key))
    rows.append(
        f"{'overall (faults only)':40} {overall.accuracy():>13} {overall.wrong:>5} "
        f"{overall.no_verdict:>10} {overall.no_alert:>8} {overall.setup_failed:>12} "
        f"{overall.agent_error:>11}"
    )
    shas = sorted({r.sha for r in records})
    rows.append(f"git: {', '.join(shas)}")
    return "\n".join(rows)
