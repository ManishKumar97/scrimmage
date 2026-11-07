"""The derail command: derail test and derail check.

With --json, standard output holds one JSON document and nothing else. Every command's
document starts with the same envelope; schemas/cli.v1.json describes it.
"""

import argparse
import importlib.metadata
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from derail_sdk import config as settings
from derail_sdk import plan as planner
from derail_sdk import runner as running
from derail_sdk.errors import ConfigError
from derail_sdk.runner import INFRA, MISTAKE, OK, Interrupted, Runner, plural

ENVELOPE_VERSION = 1
LISTS = (running.MISTAKES, running.NOTES, running.WARNINGS, running.INFRA_ERRORS)
# The word for each exit code, per command. A command never gives a code it has no word for.
STATUS = {
    "test": {OK: "passed", running.ROW_FAILED: "failed", MISTAKE: "mistake", INFRA: "infra"},
    "check": {OK: "valid", MISTAKE: "mistake", INFRA: "infra"},
}
INTERRUPTED = "stopped by SIGINT"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="derail")
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="run every test in derail.toml")
    check = commands.add_parser("check", help="check derail.toml without running anything")
    for command in (test, check):
        command.add_argument("path", nargs="?", default="derail.toml", type=Path)
        command.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(argv)
    run = run_test if args.command == "test" else run_check
    if not args.json:
        return run(args.path, None)
    found = Found(args.command, str(args.path))
    try:
        code = run(args.path, found)
    except KeyboardInterrupt:  # before the runner's own handler is set, or after it is reset
        code = found.fail(INTERRUPTED)
    except Exception as exc:  # Derail's own failure still gives a document
        code = found.fail(f"derail {args.command} failed: {exc!r}")
    doc = envelope(args.command, code, found.path, found.messages, **found.fields)
    print(json.dumps(doc, indent=2))
    return code


class Found:
    """What a --json run gathers for its document, in place of printing it."""

    def __init__(self, command: str, path: str) -> None:
        self.path = path
        self.messages: list[tuple[str, str]] = []
        # A command's own fields, as they are when it stops before it has any to give.
        self.fields: dict[str, object] = (
            {
                "records": None,
                "tests": 0,
                "rows": [],
                "cost_usd": 0.0,
                "budget_usd": None,
                "stopped_by": None,
            }
            if command == "test"
            else {"summary": None}
        )

    def fail(self, message: str) -> int:
        self.messages.append((running.INFRA_ERRORS, message))
        return INFRA


def envelope(
    command: str, code: int, path: str, messages: Sequence[tuple[str, str]], **fields: object
) -> dict[str, object]:
    """The fields every command's document starts with, then its own, then its messages."""
    doc: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "derail": importlib.metadata.version("derail"),
        "command": command,
        "exit": code,
        "status": STATUS[command][code],
        "config": path,
        **fields,
    }
    for name in LISTS:
        doc[name] = [{"message": text} for kind, text in messages if kind == name]
    return doc


def read(path: Path) -> tuple[settings.Config | None, list[str]]:
    """The file, or its problems: the one reading both derail test and derail check do."""
    try:
        return settings.load(path.resolve()), []
    except ConfigError as exc:
        return None, list(exc.problems)


def say(problems: list[str], err: TextIO) -> None:
    for problem in problems:
        print(f"derail: {problem}", file=err)


def run_test(path: Path, found: Found | None) -> int:
    config, problems = read(path)
    if config is None:
        if found is None:
            say(problems, sys.stderr)
            print("derail: the test stopped before the first run", file=sys.stderr)
        else:
            found.messages += [(running.MISTAKES, problem) for problem in problems]
        return MISTAKE
    runner: Runner | None = None
    results: list[running.RunResult] = []
    stopped: str | None = None
    try:
        runner = Runner(config)
        results = runner.run_all()
    except Interrupted as exc:
        stopped = f"stopped by {exc}; every run was stopped"
    except Exception as exc:  # the runner's own failure is never a result
        stopped = f"infrastructure error in the runner: {exc!r}"
    finally:
        if runner is not None:
            runner.server.server.server_close()
    if stopped is not None:
        if found is None:
            print(f"derail: {stopped}", file=sys.stderr)
            return INFRA
        found.fields.update(
            records=None if runner is None else str(runner.folder),
            tests=len(config.tests),
            cost_usd=0.0 if runner is None else round(runner.ledger.spent, 8),
            budget_usd=config.agent.max_cost_usd,
        )
        return found.fail(stopped)
    assert runner is not None
    report = running.build_report(config, results, runner.folder, runner)
    if found is None:
        return running.print_report(report, sys.stdout)
    found.messages += report.messages
    found.fields.update(running.report_json(report))
    return report.code


def run_check(path: Path, found: Found | None) -> int:
    """The checks derail test makes before its first run. It starts nothing."""
    try:
        config, problems = read(path)
    except Exception as exc:  # Derail's own failure, not a mistake in the file
        if found is not None:
            return found.fail(f"infrastructure error in derail check: {exc!r}")
        print(f"derail: infrastructure error in derail check: {exc!r}", file=sys.stderr)
        print("derail check: the file was not checked; nothing was run")
        return INFRA
    if config is None:
        if found is not None:
            found.messages += [(running.MISTAKES, problem) for problem in problems]
            return MISTAKE
        say(problems, sys.stderr)
        print(f"derail check: {plural(len(problems), 'problem')}; nothing was run")
        return MISTAKE
    agent = config.agent
    rows = len(planner.rows(config))
    summary: dict[str, object] = {
        "tests": len(config.tests),
        "rows": rows,
        "runs": rows * agent.runs,
        "tools": len(agent.tools),
        "budget_usd": agent.max_cost_usd,
    }
    if found is not None:
        found.fields["summary"] = summary
        return OK
    tools = plural(len(agent.tools), "tool") if agent.tools else "no tools list"
    budget = "no budget" if agent.max_cost_usd is None else f"a budget of ${agent.max_cost_usd:g}"
    print(
        f"derail check: {path} is valid: {plural(len(config.tests), 'test')}, "
        f"{plural(rows, 'row')}, {plural(rows * agent.runs, 'run')}, {tools}, {budget}"
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
