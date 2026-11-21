"""The prices-check workflow only watches the page and opens an issue.

It never writes a price, pushes, commits or opens a PR.
"""

import html
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "prices-check.yml"
SHA_PIN = r"[\w./-]+@[0-9a-f]{40}"
PERMISSIONS = {"contents": "read", "issues": "write"}


def load() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job["steps"]]


def script_step() -> dict[str, Any]:
    (step,) = [s for s in steps(load()) if s.get("name") == "Fetch the page and compare"]
    return step


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


def test_the_job_has_a_timeout_and_the_fetch_has_a_time_limit():
    (job,) = load()["jobs"].values()
    assert job["timeout-minutes"] == 10
    assert "--max-time 60" in WORKFLOW.read_text()


def test_the_checkout_keeps_no_credentials():
    (checkout,) = [s for s in steps(load()) if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False


def test_the_issue_titles_are_fixed_and_one_command_creates_issues():
    assert WORKFLOW.read_text().count("gh issue create") == 1
    env = script_step()["env"]
    assert env["TITLE"] == "OpenAI's pricing page changed: check prices.toml"
    assert env["FETCH_TITLE"] == "Prices check cannot fetch OpenAI's pricing page"


# The run script, executed against stub curl and gh commands.

TABLE = '{"tier":[0,"standard"],"rows":[1,[[1,[[0,"model-a"],[0,1],[0,2]]]]]}'
STUB_CURL = """#!/bin/bash
while [ $# -gt 0 ]; do
  [ "$1" = "--output" ] && out="$2"
  shift
done
[ "$STUB_CURL_RC" = 0 ] && cp "$STUB_PAGE" "$out"
printf '%s' "$STUB_CURL_STATUS"
exit "$STUB_CURL_RC"
"""
STUB_GH = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
if args[:2] == ["issue", "list"]:
    with open(os.environ["STUB_ISSUES"]) as f:
        issues = json.load(f)
    if "--author" in args:
        author = args[args.index("--author") + 1]
        issues = [i for i in issues if i["author"] == author]
    print(json.dumps([{"title": i["title"]} for i in issues]))
elif args[:2] == ["issue", "create"]:
    entry = {"title": args[args.index("--title") + 1], "body": args[args.index("--body") + 1]}
    with open(os.environ["STUB_CREATED"], "a") as log:
        log.write(json.dumps(entry) + "\\n")
else:
    sys.exit("unexpected gh call: " + " ".join(args))
"""


def island(props: str) -> str:
    return f'<astro-island props="{html.escape(props)}"></astro-island>'


class Result(NamedTuple):
    code: int
    created: list[dict[str, str]]
    out: str


def run_script(
    tmp_path: Path,
    *,
    page: str,
    committed: str,
    issues: list[dict[str, str]],
    curl_rc: int = 0,
    status: str = "200",
    fingerprint_script: str | None = None,
) -> Result:
    bin_dir = tmp_path / "bin"
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    bin_dir.mkdir()
    for name, text in (("curl", STUB_CURL), ("gh", STUB_GH)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    source = fingerprint_script or (ROOT / "tools" / "pricing_fingerprint.py").read_text()
    (work / "tools" / "pricing_fingerprint.py").write_text(source)
    (work / "tools" / "pricing-fingerprint.txt").write_text(f"{committed} 2026-01-01\n")
    (tmp_path / "page.html").write_text(page)
    (tmp_path / "issues.json").write_text(json.dumps(issues))
    (tmp_path / "script.sh").write_text(script_step()["run"])
    env = {k: ("x" if "${{" in str(v) else str(v)) for k, v in script_step()["env"].items()}
    env |= {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_PAGE": str(tmp_path / "page.html"),
        "STUB_ISSUES": str(tmp_path / "issues.json"),
        "STUB_CREATED": str(tmp_path / "created.jsonl"),
        "STUB_CURL_RC": str(curl_rc),
        "STUB_CURL_STATUS": status,
    }
    done = subprocess.run(
        ["bash", str(tmp_path / "script.sh")], cwd=work, env=env, capture_output=True, text=True
    )
    log = tmp_path / "created.jsonl"
    created = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return Result(done.returncode, created, done.stdout + done.stderr)


def current_hash() -> str:
    spec = importlib.util.spec_from_file_location(
        "pricing_fingerprint", ROOT / "tools" / "pricing_fingerprint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = module.fingerprint(island(TABLE))
    assert isinstance(digest, str)
    return digest


CHANGE_TITLE = "OpenAI's pricing page changed: check prices.toml"
FETCH_TITLE = "Prices check cannot fetch OpenAI's pricing page"
BOT = "app/github-actions"
OLD = "0" * 64


@pytest.mark.slow
def test_a_changed_hash_opens_one_change_issue(tmp_path: Path):
    result = run_script(tmp_path, page=island(TABLE), committed=OLD, issues=[])
    assert result.code == 0, result.out
    assert [i["title"] for i in result.created] == [CHANGE_TITLE]
    body = result.created[0]["body"]
    assert OLD in body
    assert current_hash() in body
    assert "https://developers.openai.com/api/docs/pricing" in body


@pytest.mark.slow
def test_a_changed_hash_with_an_open_bot_issue_opens_none(tmp_path: Path):
    issues = [{"title": CHANGE_TITLE, "author": BOT}]
    result = run_script(tmp_path, page=island(TABLE), committed=OLD, issues=issues)
    assert result.code == 0, result.out
    assert result.created == []
    assert "already open" in result.out


@pytest.mark.slow
def test_an_open_issue_by_another_author_does_not_mute_the_alert(tmp_path: Path):
    issues = [{"title": CHANGE_TITLE, "author": "someone-else"}]
    result = run_script(tmp_path, page=island(TABLE), committed=OLD, issues=issues)
    assert result.code == 0, result.out
    assert [i["title"] for i in result.created] == [CHANGE_TITLE]


@pytest.mark.slow
def test_an_unchanged_hash_opens_no_issue(tmp_path: Path):
    result = run_script(tmp_path, page=island(TABLE), committed=current_hash(), issues=[])
    assert result.code == 0, result.out
    assert result.created == []


@pytest.mark.slow
def test_a_page_without_pricing_tables_opens_the_change_issue(tmp_path: Path):
    result = run_script(tmp_path, page="<html></html>", committed=OLD, issues=[])
    assert result.code == 0, result.out
    assert [i["title"] for i in result.created] == [CHANGE_TITLE]
    assert "Page shape changed" in result.created[0]["body"]


@pytest.mark.slow
def test_a_failed_fetch_opens_one_fetch_issue_and_fails_the_job(tmp_path: Path):
    result = run_script(tmp_path, page="", committed=OLD, issues=[], curl_rc=22, status="403")
    assert result.code == 1
    assert [i["title"] for i in result.created] == [FETCH_TITLE]
    body = result.created[0]["body"]
    assert "curl exit code: 22" in body
    assert "HTTP status: 403" in body


@pytest.mark.slow
def test_a_failed_fetch_with_an_open_bot_issue_opens_none_but_still_fails(tmp_path: Path):
    issues = [{"title": FETCH_TITLE, "author": BOT}]
    result = run_script(tmp_path, page="", committed=OLD, issues=issues, curl_rc=28, status="000")
    assert result.code == 1
    assert result.created == []


@pytest.mark.slow
def test_a_crashing_fingerprint_script_fails_the_job_and_opens_no_issue(tmp_path: Path):
    result = run_script(
        tmp_path,
        page=island(TABLE),
        committed=OLD,
        issues=[],
        fingerprint_script="import sys\nsys.exit(2)\n",
    )
    assert result.code == 2
    assert result.created == []
