"""The unsupported value check on the lab's saved incident runs.

The runs and their scores are not in the repo, so these tests skip without them.
Point FAULTLINE_SAVED_RUNS at a directory holding `incidents-full.jsonl`, one record per
trial with `correct` and `trace_file`, and the span files under
`traces/incidents-full/`. The default is `runs/` in the repo root.
"""

import json
import os
from pathlib import Path

import pytest

from faultline.checks import unsupported_value
from faultline.config import load_config
from faultline.detect import detect
from faultline.sources import FileSource

ROOT = Path(__file__).parents[1]
SAVED = Path(os.environ.get("FAULTLINE_SAVED_RUNS", ROOT / "runs"))
RECORDS = SAVED / "incidents-full.jsonl"
TRACES = SAVED / "traces" / "incidents-full"

pytestmark = pytest.mark.skipif(
    not RECORDS.is_file(), reason=f"no saved incident runs at {SAVED}"
)


def judge_by_score() -> tuple[dict[bool, int], dict[bool, list[str]]]:
    """How many runs the check judged, and the span files of those it flagged, by
    whether the run was scored correct."""
    config = load_config(ROOT / "lab/faultline.toml")
    judged = {True: 0, False: 0}
    flagged: dict[bool, list[str]] = {True: [], False: []}
    for line in RECORDS.read_text().splitlines():
        record = json.loads(line)
        if not record["trace_file"]:
            continue
        path = TRACES / Path(record["trace_file"]).name
        if not path.is_file():
            continue
        for run in FileSource(path, config.attributes).runs():
            faults = detect(run, config.detect)
            findings = unsupported_value(run, faults, config.checks)
            judged[record["correct"]] += len(findings)
            if any(f.flagged for f in findings):
                flagged[record["correct"]].append(path.name)
    return judged, flagged


def test_no_run_scored_correct_is_flagged():
    judged, flagged = judge_by_score()

    print(f"judged {judged}; flagged {len(flagged[False])} wrong: {flagged[False]}")
    assert judged[True] == 14  # every correct run has an answer to judge
    assert flagged[True] == []
    # Known, not a goal: on these runs the check finds nothing in the wrong runs
    # either. A change here means the rule changed; look at which run moved.
    assert judged[False] == 7
    assert flagged[False] == []
