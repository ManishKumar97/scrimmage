"""The scheduled jobs that watch the gated libraries cover every library and every gate test."""

import importlib.util
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.version import Version

import tested_ranges

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_TESTS = ROOT / "tools" / "gate-tests.txt"
RANGE_TEST = "tests/test_tested_range.py"
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


def test_the_newest_job_runs_the_whole_gate_list() -> None:
    step = "The gate tests, with the newest releases"
    assert run_text("newest.yml", step) == "uv run --no-sync pytest $(python3 tools/gate_tests.py)"


def next_jobs() -> dict[str, Any]:
    return load("next-releases.yml")["jobs"]


def test_the_next_releases_job_only_warns() -> None:
    workflow = load("next-releases.yml")
    assert set(triggers(workflow)) == {"schedule", "workflow_dispatch"}, "never a pull request"
    assert workflow["permissions"] == {"contents": "read"}
    used = [s for job in next_jobs().values() for s in job["steps"] if "uses" in s]
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", s["uses"]) for s in used)
    checkouts = [s for s in used if "checkout" in s["uses"]]
    assert checkouts
    assert all(s["with"].get("persist-credentials") is False for s in checkouts)


def test_the_next_releases_job_never_uses_the_shared_cache() -> None:
    """It runs other projects' code, and the cache key is shared with the trusted jobs."""
    steps = [s for job in next_jobs().values() for s in job["steps"]]
    setups = [s for s in steps if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert len(setups) == 2
    assert all(s["with"]["enable-cache"] is False for s in setups)
    # Nothing else may write or read a cache or an artifact a trusted job could restore.
    shared = ("actions/cache", "actions/upload-artifact", "actions/download-artifact")
    assert not [s for s in steps if s.get("uses", "").startswith(shared)]
    for name, job in next_jobs().items():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, name


def test_the_pages_build_never_uses_the_shared_cache() -> None:
    steps = load("pages.yml")["jobs"]["build"]["steps"]
    (setup,) = [s for s in steps if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup["with"]["enable-cache"] is False


def gate_list(capsys: pytest.CaptureFixture[str], *flags: str) -> list[str]:
    path = ROOT / "tools" / "gate_tests.py"
    spec = importlib.util.spec_from_file_location("gate_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    capsys.readouterr()
    assert module.main(list(flags)) == 0
    return capsys.readouterr().out.split()


def test_each_leg_installs_alone_and_runs_the_gate_tests_without_the_range_check() -> None:
    next_job = next_jobs()["next"]
    assert next_job["strategy"]["fail-fast"] is False
    assert "fromJSON(needs.legs.outputs.matrix)" in next_job["strategy"]["matrix"]["leg"]
    install = run_text("next-releases.yml", "Install this leg's next version past the lock")
    assert "uv pip install -r leg-requirements.txt" in install
    assert "--prerelease allow" not in install
    assert "uv pip freeze" in install
    gate = run_text("next-releases.yml", "The gate tests, except the range check")
    command = "uv run --no-sync pytest $(python3 tools/gate_tests.py --without-range)"
    assert gate == command, "the one place that drops the range check"
    assert command.removeprefix("uv run --no-sync pytest ") in (ROOT / "RELEASING.md").read_text()


def test_the_gate_list_script_drops_only_the_range_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    everything = gate_list(capsys)
    assert everything == GATE_TESTS.read_text().split()
    assert gate_list(capsys, "--without-range") == [t for t in everything if t != RANGE_TEST]
    assert RANGE_TEST in everything


def next_releases() -> Any:
    path = ROOT / "tools" / "next_releases.py"
    spec = importlib.util.spec_from_file_location("next_releases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release(text: str, uploaded: str) -> Any:
    return next_releases().Release(Version(text), datetime.fromisoformat(uploaded))


NOW = datetime.fromisoformat("2026-11-28T00:00:00+00:00")


def test_a_stale_pre_release_is_skipped() -> None:
    nr = next_releases()
    # httpx today: a 1.0 build from August, a stable 0.29 uploaded after it.
    stale = [
        release("0.28.1", "2025-12-06T00:00:00+00:00"),
        release("0.29.0", "2026-11-26T00:00:00+00:00"),
        release("1.0.dev6", "2026-08-31T00:00:00+00:00"),
    ]
    assert nr.next_prerelease(stale, NOW) is None
    # Superseded: the stable release is at or above the pre-release.
    old = [
        release("1.0.0rc3", "2026-11-01T00:00:00+00:00"),
        release("3.60.0", "2026-11-20T00:00:00+00:00"),
    ]
    assert nr.next_prerelease(old, NOW) is None


def test_a_fresh_pre_release_counts() -> None:
    nr = next_releases()
    after = [
        release("0.29.0", "2026-11-26T00:00:00+00:00"),
        release("1.0.dev7", "2026-11-27T00:00:00+00:00"),
    ]
    assert nr.next_prerelease(after, NOW).version == Version("1.0.dev7")
    # A patch of the older line, uploaded after the release candidate, does not drop it.
    patched = [
        release("2.3.0", "2026-10-01T00:00:00+00:00"),
        release("2.4.0rc1", "2026-11-10T00:00:00+00:00"),
        release("2.3.1", "2026-11-20T00:00:00+00:00"),
    ]
    assert nr.next_prerelease(patched, NOW).version == Version("2.4.0rc1")
    assert nr.next_prerelease([release("0.1.0b2", "2026-01-01T00:00:00+00:00")], NOW) is not None


def test_each_library_gets_a_default_branch_leg_and_a_pre_release_leg_only_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nr = next_releases()
    commit = "a" * 40
    monkeypatch.setattr(nr, "head", lambda repo: ("main", commit))
    stable = release("1.0", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(nr, "releases", lambda name: [stable])
    (branch,) = nr.legs("langchain-openai")
    assert branch["library"] == "langchain-openai"
    assert branch["requirements"].splitlines() == [
        f"langchain-openai @ git+https://github.com/langchain-ai/langchain@{commit}"
        "#subdirectory=libs/partners/openai",
        f"langchain-core @ git+https://github.com/langchain-ai/langchain@{commit}"
        "#subdirectory=libs/core",
    ]
    assert commit in branch["source"] and "main" in branch["source"]
    monkeypatch.setattr(
        nr, "releases", lambda name: [stable, release("2.0b1", "2026-02-01T00:00:00+00:00")]
    )
    branch, pre = nr.legs("httpx")
    assert branch["requirements"] == f"httpx @ git+https://github.com/encode/httpx@{commit}"
    assert pre["requirements"] == "httpx==2.0b1"
    assert pre["name"] == "httpx, pre-release 2.0b1"


@pytest.mark.parametrize("library", [*tested_ranges.ranges(), *THROUGH_HTTPX])
def test_the_next_releases_job_covers_every_gated_library(library: str) -> None:
    assert library in next_releases().LIBRARIES


def test_the_tested_versions_table_lists_every_tested_range() -> None:
    """A widened TESTED range cannot leave the page stale, and a new gate must add its row."""
    text = (ROOT / "docs" / "limits" / "calls.md").read_text()
    section = text.split("\n## Tested versions\n")[1].split("\n## ")[0]
    cells = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith("| Library")
    ]
    shown = {library: (low, high) for library, low, high in cells}
    assert len(cells) == len(shown), "a library has two rows"
    expected = {
        library: (".".join(map(str, low)), ".".join(map(str, high)))
        for library, (low, high) in tested_ranges.ranges().items()
    }
    assert shown == expected, "run the gate tests with the new version, then edit this table"
