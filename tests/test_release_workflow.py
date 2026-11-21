"""The release workflow publishes with trusted publishing and holds no secret."""

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
PUBLISH_JOBS = {"testpypi": "testpypi", "pypi": "pypi"}


def load() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job["steps"]]


def test_only_a_v_tag_triggers_the_release():
    assert triggers(load()) == {"push": {"tags": ["v*"]}}


def test_only_the_publish_jobs_can_get_an_oidc_token():
    workflow = load()
    assert "id-token" not in (workflow.get("permissions") or {}), "no workflow-wide token"
    holders = {
        name
        for name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    }
    assert holders == set(PUBLISH_JOBS)
    for name in PUBLISH_JOBS:
        assert workflow["jobs"][name]["permissions"] == {"id-token": "write"}
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}


def test_each_publish_job_names_its_environment():
    jobs = load()["jobs"]
    for name, environment in PUBLISH_JOBS.items():
        assert jobs[name]["environment"] == environment


def test_a_release_candidate_never_reaches_pypi():
    jobs = load()["jobs"]
    condition = str(jobs["pypi"].get("if"))
    assert "!contains(github.ref_name, 'rc')" in condition
    assert "if" not in jobs["testpypi"]
    assert jobs["pypi"]["needs"] == "testpypi"


def test_publishing_follows_the_build_and_uses_the_built_files():
    jobs = load()["jobs"]
    assert jobs["testpypi"]["needs"] == "build"
    for name in PUBLISH_JOBS:
        uses = [s["uses"] for s in jobs[name]["steps"]]
        assert any(u.startswith("actions/download-artifact@") for u in uses)
        assert any(u.startswith("pypa/gh-action-pypi-publish@") for u in uses)


def test_no_step_uses_a_cache_or_a_secret():
    workflow = load()
    assert not any(s.get("uses", "").startswith("actions/cache") for s in steps(workflow))
    for s in steps(workflow):
        assert s.get("with", {}).get("enable-cache") in (None, False), s
    text = WORKFLOW.read_text()
    secrets = re.findall(r"secrets\.\w+|secrets\[", text)
    assert not secrets, "the workflow must hold no secret"
    assert "password" not in text.lower()


def test_every_action_is_pinned_to_a_full_sha():
    used = [s["uses"] for s in steps(load()) if "uses" in s]
    assert used
    unpinned = [u for u in used if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u)]
    assert not unpinned, f"pin by full SHA: {unpinned}"
