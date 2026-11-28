"""The scheduled jobs that watch the gated libraries cover every library and every gate test."""

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.version import Version

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


def test_both_jobs_run_the_listed_gate_tests() -> None:
    for name, step in [
        ("newest.yml", "The gate tests, with the newest releases"),
        ("next-releases.yml", "The gate tests, with the next releases"),
    ]:
        assert "$(cat tools/gate-tests.txt)" in run_text(name, step), name


def test_the_next_releases_job_only_warns() -> None:
    workflow = load("next-releases.yml")
    assert set(triggers(workflow)) == {"schedule", "workflow_dispatch"}, "never a pull request"
    assert workflow["permissions"] == {"contents": "read"}
    steps = [s for job in workflow["jobs"].values() for s in job["steps"]]
    used = [s for s in steps if "uses" in s]
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", s["uses"]) for s in used)
    assert all(
        s["with"].get("persist-credentials") is False for s in used if "checkout" in s["uses"]
    )
    assert "next-requirements.txt" in run_text("next-releases.yml", "Install them past the lock")


def next_releases() -> Any:
    path = ROOT / "tools" / "next_releases.py"
    spec = importlib.util.spec_from_file_location("next_releases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def versions(*texts: str) -> list[Version]:
    return sorted(Version(t) for t in texts)


def test_a_pre_release_counts_only_when_it_is_newer_than_the_newest_stable() -> None:
    nr = next_releases()
    assert nr.next_prerelease(versions("0.28.1", "0.29.0", "1.0.dev5", "1.0.dev6")) == Version(
        "1.0.dev6"
    )
    assert nr.next_prerelease(versions("1.0.0rc3", "3.60.0")) is None, "an old release candidate"
    assert nr.next_prerelease(versions("2.3.0")) is None
    assert nr.next_prerelease(versions("0.1.0a1", "0.1.0b2")) == Version("0.1.0b2"), "no stable yet"


def test_the_requirement_is_a_pre_release_or_the_default_branch_at_one_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nr = next_releases()
    commit = "a" * 40
    monkeypatch.setattr(nr, "head", lambda repo: ("main", commit))
    monkeypatch.setattr(nr, "releases", lambda name: versions("1.0", "2.0b1"))
    assert nr.requirement("lib", "o/r", "") == ("lib==2.0b1", "lib: pre-release 2.0b1 from PyPI")
    monkeypatch.setattr(nr, "releases", lambda name: versions("1.0", "1.1"))
    line, said = nr.requirement("lib", "o/r", "libs/lib")
    assert line == f"lib @ git+https://github.com/o/r@{commit}#subdirectory=libs/lib"
    assert said == f"lib: no pre-release, default branch main of o/r at commit {commit}"


@pytest.mark.parametrize("library", [*tested_ranges.ranges(), *THROUGH_HTTPX])
def test_the_next_releases_job_covers_every_gated_library(library: str) -> None:
    assert library in next_releases().LIBRARIES
