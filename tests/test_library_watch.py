"""The scheduled jobs that watch the gated libraries cover every library and every gate test."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import tested_ranges

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_TESTS = ROOT / "tools" / "gate-tests.txt"
# Gated through httpx, so they carry no range of their own, and still break with a release.
THROUGH_HTTPX = ["openai", "anthropic", "langchain-openai", "langgraph"]


def load(name: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def run_text(name: str, step_name: str) -> str:
    steps = [s for job in load(name)["jobs"].values() for s in job["steps"]]
    (step,) = [s for s in steps if s.get("name") == step_name]
    return step["run"]


def test_the_newest_job_runs_daily_by_hand_and_from_ci() -> None:
    assert set(triggers(load("newest.yml"))) == {"workflow_call", "schedule", "workflow_dispatch"}
    assert load("ci.yml")["jobs"]["newest"]["uses"] == "./.github/workflows/newest.yml"


@pytest.mark.parametrize("library", [*tested_ranges.ranges(), *THROUGH_HTTPX])
def test_the_newest_job_upgrades_every_gated_library(library: str) -> None:
    upgrade = run_text("newest.yml", "Upgrade the gated libraries past the lock")
    assert re.search(rf"(?<![\w-]){re.escape(library)}(?![\w-])", upgrade.splitlines()[0]), library


def test_every_gate_test_is_in_the_list_and_exists() -> None:
    listed = GATE_TESTS.read_text().split()
    assert listed == sorted(set(listed), key=listed.index), "a test is listed twice"
    assert all((ROOT / path).is_file() for path in listed)
    assert "tests/test_tested_range.py" in listed
