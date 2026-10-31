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


def flags_by_score() -> dict[bool, list[str]]:
    """The span files of flagged runs, by whether the run was scored correct."""
    config = load_config(ROOT / "lab/faultline.toml")
    flagged: dict[bool, list[str]] = {True: [], False: []}
    judged = 0
    for line in RECORDS.read_text().splitlines():
        record = json.loads(line)
        if not record["trace_file"]:
            continue
        path = TRACES / Path(record["trace_file"]).name
        if not path.is_file():
            continue
        for run in FileSource(path, config.attributes).runs():
            judged += 1
            faults = detect(run, config.detect)
            if any(f.flagged for f in unsupported_value(run, faults, config.checks)):
                flagged[record["correct"]].append(path.name)
    assert judged > 0
    return flagged


def test_no_run_scored_correct_is_flagged():
    flagged = flags_by_score()

    print(f"flagged: {len(flagged[False])} wrong runs {flagged[False]}")
    assert flagged[True] == []
