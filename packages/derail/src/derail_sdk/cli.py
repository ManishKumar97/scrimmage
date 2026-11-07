"""The derail command: derail test, derail check and derail schema.

With --json, standard output holds one JSON document and nothing else. Every command's
document starts with the same envelope; schemas/cli.v1.json describes it.
"""

import argparse
import importlib.metadata
import json
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from derail_sdk import config as settings
from derail_sdk import entry
from derail_sdk import plan as planner
from derail_sdk import runner as running
from derail_sdk.errors import ConfigError
from derail_sdk.runner import INFRA, MISTAKE, OK, Interrupted, Runner, plural

SCHEMAS = Path(__file__).parent / "schemas"
ENVELOPE_VERSION = 1
UNKNOWN_VERSION = "unknown"  # Derail runs from a source tree with no package metadata
LISTS = (running.MISTAKES, running.NOTES, running.WARNINGS, running.INFRA_ERRORS)
# The word for each exit code, per command. A command never gives a code it has no word for.
STATUS = {
    "test": {OK: "passed", running.ROW_FAILED: "failed", MISTAKE: "mistake", INFRA: "infra"},
    "check": {OK: "valid", MISTAKE: "mistake", INFRA: "infra"},
}
# With --json these stop the command with a document, wherever they arrive. The runner sets
# its own handlers while runs go, and puts these back when they end.
STOP_SIGNALS = entry.STOP_SIGNALS


class Stopped(BaseException):
    """derail test or check got SIGINT or SIGTERM outside the runs, with --json."""


def main(argv: list[str] | None = None, *, process: bool = False) -> int:
    """The derail command. process: this is the derail process itself, which exits on the
    code returned, so signals stay ignored after the document is printed."""
    parser = argparse.ArgumentParser(prog="derail")
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="run every test in derail.toml")
    check = commands.add_parser("check", help="check derail.toml without running anything")
    for command in (test, check):
        command.add_argument("path", nargs="?", default="derail.toml", type=Path)
        command.add_argument("--json", action="store_true", help="print one JSON document")
    commands.add_parser("schema", help="print the JSON Schema of derail.toml")
    args = parser.parse_args(argv)
    if args.command == "schema":
        sys.stdout.write((SCHEMAS / "derail-toml.v1.json").read_text())
        return OK
    run = run_test if args.command == "test" else run_check
    if not args.json:
        return run(args.path, None)
    return as_json(args.command, args.path, run, process)


def ignore_signals() -> None:
    """From here on the command's code is settled: no signal may change the exit it states."""
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)


def stop(signum: int, frame: object) -> None:
    del frame
    ignore_signals()  # a second signal must not interrupt the document of the first
    raise Stopped(signal.Signals(signum).name)


def as_json(
    command: str, path: Path, run: Callable[[Path, "Found | None"], int], process: bool
) -> int:
    """Run a command for --json: one document on standard output, whatever happens."""
    version = derail_version()
    found = Found(command, str(path))
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    try:
        try:
            for sig in STOP_SIGNALS:
                signal.signal(sig, stop)
            if entry.pending:  # it came while Derail was still loading
                raise Stopped(entry.pending.pop())
            code = run(path, found)
        except (Stopped, KeyboardInterrupt) as exc:
            code = found.fail(f"stopped by {str(exc) or 'SIGINT'}")  # Ctrl-C has no name
        except Exception as exc:  # Derail's own failure still gives a document
            code = found.fail(f"derail {command} failed: {exc!r}")
        ignore_signals()
        text = json.dumps(envelope(command, code, found, version), indent=2)
    except Exception as exc:  # the document itself could not be built
        ignore_signals()
        code = INFRA
        bare = Found(command, str(path))
        bare.fail(f"derail {command} could not write its document: {exc!r}")
        text = json.dumps(envelope(command, code, bare, version), indent=2)
    print(text, flush=True)
    # The derail process exits on this code with signals still ignored. A caller in the
    # same process, such as a test, gets its own handlers back.
    if not process:
        for sig, handler in previous.items():
            if handler is not None:  # None: set outside Python, which cannot be put back
                signal.signal(sig, handler)
    return code


def derail_version() -> str:
    try:
        return importlib.metadata.version("derail")
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


class Found:
    """What a --json run gathers for its document, in place of printing it."""

    def __init__(self, command: str, path: str) -> None:
        self.path = path
        self.messages: list[tuple[str, str]] = []
        self.runner: Runner | None = None
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

    def started(self, config: settings.Config, runner: Runner) -> None:
        """The test has a records folder: a document from here on names it."""
        self.runner = runner
        self.fields.update(
            records=str(runner.folder),
            tests=len(config.tests),
            budget_usd=config.agent.max_cost_usd,
        )

    def fail(self, message: str) -> int:
        self.messages.append((running.INFRA_ERRORS, message))
        if self.runner is not None:
            self.fields["cost_usd"] = round(self.runner.ledger.spent, 8)
        return INFRA


def envelope(command: str, code: int, found: Found, version: str) -> dict[str, object]:
    return document(command, code, found.path, found.messages, version, **found.fields)


def document(
    command: str,
    code: int,
    path: str,
    messages: Sequence[tuple[str, str]],
    version: str,
    **fields: object,
) -> dict[str, object]:
    """The fields every command's document starts with, then its own, then its messages."""
    doc: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "derail": version,
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


def unreadable(command: str, exc: Exception, found: Found | None) -> int:
    """Reading the file failed inside Derail, as a parser that runs out of stack does."""
    message = f"infrastructure error in derail {command}: {exc!r}"
    if found is not None:
        return found.fail(message)
    print(f"derail: {message}", file=sys.stderr)
    last = "the file was not checked" if command == "check" else "the test did not start"
    print(f"derail {command}: {last}; nothing was run")
    return INFRA


def run_test(path: Path, found: Found | None) -> int:
    try:
        config, problems = read(path)
    except Exception as exc:  # Derail's own failure, not a mistake in the file
        return unreadable("test", exc, found)
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
        if found is not None:
            found.started(config, runner)
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
        return found.fail(stopped)
    assert runner is not None
    report = running.build_report(config, results, runner.folder, runner)
    if found is None:
        return running.print_report(report, sys.stdout)
    found.messages += report.every_message
    found.fields.update(running.report_json(report))
    return report.code


def run_check(path: Path, found: Found | None) -> int:
    """The checks derail test makes before its first run. It starts nothing."""
    try:
        config, problems = read(path)
    except Exception as exc:  # Derail's own failure, not a mistake in the file
        return unreadable("check", exc, found)
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
    sys.exit(main(process=True))
