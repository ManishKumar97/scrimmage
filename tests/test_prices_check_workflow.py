"""The prices-check workflow only watches the page and opens an issue.

It never writes a price, pushes, commits or opens a PR.
"""

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "prices-check.yml"
SHA_PIN = r"[\w./-]+@[0-9a-f]{40}"
PERMISSIONS = {"contents": "read", "issues": "write"}


def load() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job["steps"]]


def test_the_triggers_are_exactly_a_weekly_schedule_and_manual_dispatch():
    on = triggers(load())
    assert set(on) == {"schedule", "workflow_dispatch"}
    (entry,) = on["schedule"]
    assert re.fullmatch(r"\d+ \d+ \* \* \d", entry["cron"]), "weekly: fixed minute, hour, weekday"


def test_the_permissions_are_exactly_contents_read_and_issues_write():
    workflow = load()
    assert workflow["permissions"] == PERMISSIONS
    for name, job in workflow["jobs"].items():
        assert job.get("permissions", PERMISSIONS) == PERMISSIONS, name


def test_the_workflow_never_pushes_commits_or_opens_a_pr():
    text = WORKFLOW.read_text()
    found = [w for w in ("git push", "git commit", "gh pr") if w in text]
    assert not found, f"the workflow must not write: {found}"


def test_every_action_is_pinned_to_a_full_sha():
    used = [s["uses"] for s in steps(load()) if "uses" in s]
    assert used
    unpinned = [u for u in used if not re.fullmatch(SHA_PIN, u)]
    assert not unpinned, f"pin by full SHA: {unpinned}"


def test_the_only_secret_is_the_github_token_for_gh():
    text = WORKFLOW.read_text()
    assert "secrets.GITHUB_TOKEN" in text
    others = re.findall(r"secrets\b(?!\.GITHUB_TOKEN\b)", text, re.IGNORECASE)
    assert not others, f"no secret but GITHUB_TOKEN: {others}"


def test_a_change_or_a_new_page_shape_opens_one_issue_with_the_fixed_title():
    text = WORKFLOW.read_text()
    assert text.count("gh issue create") == 1
    assert "OpenAI's pricing page changed: check prices.toml" in text
    assert "gh issue list" in text, "no second issue while one is open"
