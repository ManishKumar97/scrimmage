"""The release workflow publishes with trusted publishing and holds no secret."""

import re
import textwrap
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
PUBLISH_JOBS = {"testpypi": "testpypi", "pypi": "pypi"}
SHA_PIN = r"[\w./-]+@[0-9a-f]{40}"


def load() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job["steps"]]


def uses_of(step_list: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [s for s in step_list if s.get("uses", "").startswith(prefix)]


def test_only_a_v_tag_triggers_the_release():
    assert triggers(load()) == {"push": {"tags": ["v*"]}}


def test_only_the_publish_jobs_can_get_an_oidc_token():
    workflow = load()
    assert workflow["permissions"] == {}, "no workflow-wide permission"
    holders = {
        name
        for name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    }
    assert holders == set(PUBLISH_JOBS)
    for name in PUBLISH_JOBS:
        assert workflow["jobs"][name]["permissions"] == {"id-token": "write"}
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["check"]["permissions"] == {}


def test_each_publish_job_names_its_environment():
    jobs = load()["jobs"]
    for name, environment in PUBLISH_JOBS.items():
        assert jobs[name]["environment"] == environment


def test_a_release_candidate_never_reaches_pypi():
    jobs = load()["jobs"]
    assert jobs["pypi"]["if"] == "${{ needs.build.outputs.prerelease == 'false' }}"
    assert "build" in jobs["pypi"]["needs"]
    assert "testpypi" in jobs["pypi"]["needs"]
    assert "if" not in jobs["testpypi"]
    assert jobs["build"]["outputs"] == {"prerelease": "${{ steps.version.outputs.prerelease }}"}


def run_version_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, tag: str
) -> tuple[bool, str, str]:
    """Run the workflow's own version script on a fake wheel. Return passed, output, message."""
    (step,) = [s for s in load()["jobs"]["build"]["steps"] if s.get("id") == "version"]
    assert step["env"] == {"TAG": "${{ github.ref_name }}"}
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    with zipfile.ZipFile(work / "dist" / f"scrimmage-{version}-py3-none-any.whl", "w") as z:
        z.writestr(
            f"scrimmage-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nVersion: {version}\n",
        )
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.chdir(work)
    monkeypatch.setenv("TAG", tag)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    script = textwrap.dedent(step["run"].split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0])
    try:
        exec(compile(script, "version-step", "exec"), {"__name__": "__main__"})
    except SystemExit as stop:
        return False, output.read_text(), str(stop)
    return True, output.read_text(), ""


@pytest.mark.parametrize(
    ("version", "prerelease"),
    [("0.1.0", "false"), ("10.20.30", "false"), ("0.1.0rc1", "true"), ("0.1.0rc12", "true")],
)
def test_the_version_step_passes_final_and_rc_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, prerelease: str
):
    passed, output, err = run_version_step(tmp_path, monkeypatch, version, f"v{version}")
    assert passed, err
    assert output == f"prerelease={prerelease}\n"


@pytest.mark.parametrize(
    "version",
    ["0.1.0c1", "0.1.0a1", "0.1.0b1", "0.1.0.dev0", "0.1.0-rc1", "0.1.0rc", "0.1", "0.1.0.post1"],
)
def test_the_version_step_refuses_every_other_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
):
    passed, output, _ = run_version_step(tmp_path, monkeypatch, version, f"v{version}")
    assert not passed
    assert output == "", "a refused version must not set the prerelease output"


@pytest.mark.parametrize("tag", ["v0.1.1", "0.1.0", "V0.1.0", "v0.1.0rc1"])
def test_the_version_step_refuses_a_tag_that_differs_from_the_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str
):
    passed, output, err = run_version_step(tmp_path, monkeypatch, "0.1.0", tag)
    assert not passed
    assert "bump the version" in err
    assert output == ""


def test_the_tagged_commit_must_be_on_main():
    build = load()["jobs"]["build"]["steps"]
    (checkout,) = uses_of(build, "actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    runs = [s["run"] for s in build if "run" in s]
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in runs


def test_the_build_is_reproducible_and_uses_exact_build_tools():
    run = next(s["run"] for s in load()["jobs"]["build"]["steps"] if "uv build" in s.get("run", ""))
    assert 'SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"' in run
    assert "export SOURCE_DATE_EPOCH" in run
    assert "uv build --package scrimmage" in run
    assert "--build-constraints packages/scrimmage/build-constraints.txt" in run
    assert "--require-hashes" in run
    constraints = (
        WORKFLOW.parent.parent.parent / "packages/scrimmage/build-constraints.txt"
    ).read_text()
    assert "hatchling==1.33.0 " in constraints
    assert (
        'requires = ["hatchling==1.33.0"]'
        in (WORKFLOW.parent.parent.parent / "packages/scrimmage/pyproject.toml").read_text()
    )


def test_the_package_is_checked_in_a_job_that_cannot_change_it():
    jobs = load()["jobs"]
    check = jobs["check"]
    assert check["needs"] == "build"
    runs = [s["run"] for s in check["steps"] if "run" in s]
    assert runs == ["uvx twine@6.2.0 check --strict dist/*"]
    assert uses_of(check["steps"], "actions/download-artifact@")
    assert not uses_of(check["steps"], "actions/checkout@")
    assert not any("twine" in s.get("run", "") for s in jobs["build"]["steps"])
    for name in PUBLISH_JOBS:
        assert "check" in jobs[name]["needs"]
        assert "build" in jobs[name]["needs"]


def test_publishing_uses_the_built_files_with_attestations():
    jobs = load()["jobs"]
    for name in PUBLISH_JOBS:
        job_steps = jobs[name]["steps"]
        assert uses_of(job_steps, "actions/download-artifact@")
        (publish,) = uses_of(job_steps, "pypa/gh-action-pypi-publish@")
        assert publish["with"]["attestations"] is True
        assert [s for s in job_steps if "run" in s] == [], "a publish job runs no shell"
    assert (
        jobs["testpypi"]["steps"][-1]["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    )
    assert "repository-url" not in jobs["pypi"]["steps"][-1]["with"]


def test_no_step_uses_a_cache_or_a_secret():
    workflow = load()
    assert not uses_of(steps(workflow), "actions/cache")
    setups = uses_of(steps(workflow), "astral-sh/setup-uv@")
    assert setups
    for s in setups:
        assert s["with"].get("enable-cache") is False, "setup-uv caches unless told not to"
    text = WORKFLOW.read_text()
    secrets = re.findall(r"\bsecrets\b", text, re.IGNORECASE)
    assert not secrets, "the workflow must hold no secret"
    assert "password" not in text.lower()


def test_the_tag_reaches_scripts_only_through_the_environment():
    workflow = load()
    for s in steps(workflow):
        assert "${{" not in s.get("run", ""), f"expression inside a script: {s['run']}"
    (step,) = [s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "version"]
    assert step["env"]["TAG"] == "${{ github.ref_name }}"


def test_checkouts_keep_no_credentials():
    checkouts = uses_of(steps(load()), "actions/checkout@")
    assert checkouts
    for s in checkouts:
        assert s["with"]["persist-credentials"] is False


def test_every_action_is_pinned_to_a_full_sha():
    used = [s["uses"] for s in steps(load()) if "uses" in s]
    assert used
    unpinned = [u for u in used if not re.fullmatch(SHA_PIN, u)]
    assert not unpinned, f"pin by full SHA: {unpinned}"
