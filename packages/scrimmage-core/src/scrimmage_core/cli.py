"""The scrimmage command: scrimmage test, check, record, init and schema.

With --json, standard output holds one JSON document and nothing else. Every command's
document starts with the same envelope; schemas/cli.v1.json describes it.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from scrimmage_core import config as settings
from scrimmage_core import entry
from scrimmage_core import plan as planner
from scrimmage_core.errors import ConfigError
from scrimmage_core.outcomes import (
    INFRA,
    INFRA_ERRORS,
    MISTAKE,
    MISTAKES,
    NOTES,
    OK,
    ROW_FAILED,
    WARNINGS,
    plural,
)

if TYPE_CHECKING:
    from scrimmage_core.runner import Runner

SCHEMAS = Path(__file__).parent / "schemas"
ENVELOPE_VERSION = 1
UNKNOWN_VERSION = "unknown"  # Scrimmage runs from a source tree with no package metadata
LISTS = (MISTAKES, NOTES, WARNINGS, INFRA_ERRORS)
# The word for each exit code, per command. A command never gives a code it has no word for.
STATUS = {
    "test": {OK: "passed", ROW_FAILED: "failed", MISTAKE: "mistake", INFRA: "infra"},
    "check": {OK: "valid", MISTAKE: "mistake", INFRA: "infra"},
    "record": {
        OK: "complete",
        ROW_FAILED: "incomplete",
        MISTAKE: "mistake",
        INFRA: "infra",
    },
    "init": {OK: "written", MISTAKE: "mistake", INFRA: "infra"},
}
# With --json these stop the command with a document, wherever they arrive. The runner sets
# its own handlers while runs go, and puts these back when they end.
STOP_SIGNALS = entry.STOP_SIGNALS


class Stopped(BaseException):
    """scrimmage test or check got SIGINT or SIGTERM outside the runs, with --json.

    The signal handler never raises it: it only notes the signal, and stop_point raises it
    in plain code. An exception raised in a handler that fires inside a __del__, a weakref
    callback or a generator's close is printed and dropped by Python, and the signal lost.
    """


def main(argv: list[str] | None = None, *, process: bool = False) -> int:
    """The scrimmage command. process: this is the scrimmage process itself, which exits on the
    code returned, so signals stay ignored after the document is printed."""
    # No abbreviations: entry.py looks for --json itself, so --js would skip its handlers.
    parser = argparse.ArgumentParser(prog="scrimmage", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="run every test in scrimmage.toml", allow_abbrev=False)
    check = commands.add_parser(
        "check", help="check scrimmage.toml without running anything", allow_abbrev=False
    )
    record = commands.add_parser(
        "record", help="run each test's baseline and write its calls as mocks", allow_abbrev=False
    )
    for command in (test, check, record):
        command.add_argument("path", nargs="?", default="scrimmage.toml", type=Path)
        command.add_argument("--json", action="store_true", help="print one JSON document")
    record.add_argument(
        "--test", action="append", default=[], metavar="NAME", help="record only this test"
    )
    record.add_argument(
        "--refresh", action="append", default=[], metavar="LABEL", help="record this label again"
    )
    record.add_argument("--runs", default="1", metavar="N", help="run each test's baseline N times")
    commands.add_parser("schema", help="print the JSON Schema of scrimmage.toml")
    init = commands.add_parser(
        "init", help="draft a scrimmage.toml by reading the code", allow_abbrev=False
    )
    init.add_argument("folder", nargs="?", default=Path(), type=Path)
    init.add_argument("--out", type=Path, help="the file to write; default scrimmage.toml there")
    init.add_argument(
        "--probe",
        metavar="MODULE:FUNCTION",
        help="run this function up to its first model request, to read the tools it offers",
    )
    init.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(argv)
    if args.command == "init":
        return init_command(args, process)
    if args.command == "schema":
        sys.stdout.write((SCHEMAS / "scrimmage-toml.v1.json").read_text())
        return OK
    run = chosen_command(args)
    if not args.json:
        return run(args.path, None)
    return as_json(args.command, args.path, run, process)


def chosen_command(args: argparse.Namespace) -> Callable[[Path, Found | None], int]:
    """The function that runs this command, with a command's own flags already bound."""
    if args.command == "test":
        return run_test
    if args.command == "check":
        return run_check
    names, refresh, runs = list(args.test), list(args.refresh), str(args.runs)

    def record(path: Path, found: Found | None) -> int:
        return run_record(path, found, names, refresh, runs)

    return record


def settle() -> None:
    """From here on the command's code is settled: no signal may change the exit it states.

    A signal noted after the last stop point is dropped with it, so a later call in the
    same process does not start stopped.
    """
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    entry.pending.clear()


# With --json, a signal is noted, as while Scrimmage loads, and acted on at the next stop point.
stop = entry.note


def stop_point() -> None:
    """Stop here if a signal was noted. Called in plain code, where nothing can swallow it."""
    if entry.pending:
        raise Stopped(entry.pending[0])


def as_json(
    command: str, path: Path, run: Callable[[Path, Found | None], int], process: bool
) -> int:
    """Run a command for --json: one document on standard output, whatever happens."""
    version = scrimmage_version()
    found = Found(command, str(path))
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    try:
        try:
            for sig in STOP_SIGNALS:
                signal.signal(sig, stop)
            stop_point()  # a signal that came while Scrimmage was still loading
            code = run(path, found)
            stop_point()  # one that came after the last stop point inside the command
        except (Stopped, KeyboardInterrupt) as exc:
            code = found.fail(f"stopped by {str(exc) or 'SIGINT'}")  # Ctrl-C has no name
        except Exception as exc:  # Scrimmage's own failure still gives a document
            code = found.fail(f"scrimmage {command} failed: {exc!r}")
        settle()
        text = json.dumps(envelope(command, code, found, version), indent=2)
    except Exception as exc:  # the document itself could not be built
        settle()
        code = INFRA
        bare = Found(command, str(path))
        bare.fail(f"scrimmage {command} could not write its document: {exc!r}")
        text = json.dumps(envelope(command, code, bare, version), indent=2)
    print(text, flush=True)
    # The scrimmage process exits on this code with signals still ignored. A caller in the
    # same process, such as a test, gets its own handlers back.
    if not process:
        for sig, handler in previous.items():
            if handler is not None:  # None: set outside Python, which cannot be put back
                signal.signal(sig, handler)
    return code


def scrimmage_version() -> str:
    import importlib.metadata  # about 20 ms, so only the command that prints the version pays

    try:
        return importlib.metadata.version("scrimmage-core")
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def empty_test() -> dict[str, object]:
    return {
        "records": None,
        "tests": 0,
        "rows": [],
        "cost_usd": 0.0,
        "budget_usd": None,
        "stopped_by": None,
    }


def empty_check() -> dict[str, object]:
    return {"summary": None}


def empty_record() -> dict[str, object]:
    return {
        "out": None,
        "records": None,
        "cost_usd": 0.0,
        "numbers": {"calls_seen": 0, "calls_written": 0},
        "tests": [],
    }


def empty_init() -> dict[str, object]:
    from scrimmage_core import init as drafting  # init brings the runner, for its probe

    return drafting.empty_fields()


# A command's own fields, as they are when it stops before it has any to give.
EMPTY_FIELDS = {
    "test": empty_test,
    "check": empty_check,
    "record": empty_record,
    "init": empty_init,
}


class Found:
    """What a --json run gathers for its document, in place of printing it."""

    def __init__(self, command: str, path: str) -> None:
        self.path = path
        self.messages: list[tuple[str, str]] = []
        self.runner: Runner | None = None
        self.fields: dict[str, object] = EMPTY_FIELDS[command]()

    def started(self, config: settings.Config, runner: Runner) -> None:
        """The test has a records folder: a document from here on names it."""
        self.runner = runner
        self.fields.update(
            records=str(runner.folder),
            tests=len(config.tests),
            budget_usd=config.agent.max_cost_usd,
        )

    def fail(self, message: str) -> int:
        self.messages.append((INFRA_ERRORS, message))
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
        "scrimmage": version,
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
    """The file, or its problems: the one reading both scrimmage test and scrimmage check do."""
    try:
        return settings.load(path.resolve()), []
    except ConfigError as exc:
        return None, list(exc.problems)


def say(problems: list[str], err: TextIO) -> None:
    for problem in problems:
        print(f"scrimmage: {problem}", file=err)


def unreadable(command: str, exc: Exception, found: Found | None) -> int:
    """Reading the file failed inside Scrimmage, as a parser that runs out of stack does."""
    message = f"infrastructure error in scrimmage {command}: {exc!r}"
    if found is not None:
        return found.fail(message)
    print(f"scrimmage: {message}", file=sys.stderr)
    last = "the file was not checked" if command == "check" else "the test did not start"
    print(f"scrimmage {command}: {last}; nothing was run")
    return INFRA


def run_test(path: Path, found: Found | None) -> int:
    from scrimmage_core import runner as running  # the heavy import waits until a test runs

    try:
        config, problems = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("test", exc, found)
    stop_point()
    if config is None:
        if found is None:
            say(problems, sys.stderr)
            print("scrimmage: the test stopped before the first run", file=sys.stderr)
        else:
            found.messages += [(MISTAKES, problem) for problem in problems]
        return MISTAKE
    runner: Runner | None = None
    results: list[running.RunResult] = []
    stopped: str | None = None
    try:
        # With --json the runner shares the noted signal, so one that came just before its
        # runs start still stops them, and one during them is said once.
        runner = running.Runner(config, stop_flag=None if found is None else entry.pending)
        if found is not None:
            found.started(config, runner)
        stop_point()
        results = runner.run_all()
    except running.Interrupted as exc:
        stopped = f"stopped by {exc}; every run was stopped"
    except Exception as exc:  # the runner's own failure is never a result
        stopped = f"infrastructure error in the runner: {exc!r}"
    finally:
        if runner is not None:
            runner.server.server.server_close()
    if stopped is not None:
        if found is None:
            print(f"scrimmage: {stopped}", file=sys.stderr)
            return INFRA
        return found.fail(stopped)
    assert runner is not None
    report = running.build_report(config, results, runner.folder, runner)
    if found is None:
        return running.print_report(report, sys.stdout)
    found.messages += report.every_message
    found.fields.update(running.report_json(report))
    return report.code


def run_record(
    path: Path, found: Found | None, names: list[str], refresh: list[str], runs: str = "1"
) -> int:
    """One real baseline run per test, its real calls written as mocks beside the team's file."""
    from scrimmage_core import record as recording
    from scrimmage_core import runner as running

    try:
        config, problems = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("record", exc, found)
    stop_point()
    if config is None:
        return record_mistakes(problems, found)
    plan, problems = recording.prepare(config, names, refresh, runs)
    if plan is None:
        return record_mistakes(problems, found)
    if (before := recording.before_runs(plan.config)) is not None:
        print(f"scrimmage record: {before}", file=sys.stderr if found else sys.stdout, flush=True)
    runner: Runner | None = None
    results: list[running.RunResult] = []
    stopped: str | None = None
    try:
        runner = running.Runner(
            plan.config, stop_flag=None if found is None else entry.pending, record=True
        )
        if found is not None:
            found.runner = runner
            found.fields["records"] = str(runner.folder)
        stop_point()
        results = runner.run_all()
    except running.Interrupted as exc:
        stopped = f"stopped by {exc}; every run was stopped, and nothing was written"
    except Exception as exc:  # the runner's own failure is never a result
        stopped = f"infrastructure error in the runner: {exc!r}"
    finally:
        if runner is not None:
            runner.server.server.server_close()
    if stopped is not None:
        if found is None:
            print(f"scrimmage: {stopped}", file=sys.stderr)
            return INFRA
        return found.fail(stopped)
    assert runner is not None
    report = running.build_report(plan.config, results, runner.folder, runner, judged=False)
    stop_point()  # a signal that came while the last run ended: nothing has been written
    try:
        result = recording.finish(plan, path, runner, results, report)
        if found is not None:
            settle()  # the file is written, so no signal may turn this into a failure now
    except KeyboardInterrupt:  # Ctrl-C in the text form, where no signal is only noted
        print("scrimmage: stopped by SIGINT while the file was written", file=sys.stderr)
        return INFRA
    except Exception as exc:  # WriterBug or any other failure while writing: Scrimmage's own
        message = f"infrastructure error while writing the file, nothing was written: {exc!r}"
        if found is None:
            print(f"scrimmage: {message}", file=sys.stderr)
            return INFRA
        return found.fail(message)
    if found is None:
        recording.print_result(result, plan.config)
        return result.code
    found.messages += [*result.above, *result.messages]
    found.fields.update(recording.json_fields(result))
    return result.code


def record_mistakes(problems: list[str], found: Found | None) -> int:
    if found is None:
        say(problems, sys.stderr)
        print("scrimmage: nothing was recorded", file=sys.stderr)
        return MISTAKE
    found.messages += [(MISTAKES, problem) for problem in problems]
    return MISTAKE


def run_check(path: Path, found: Found | None) -> int:
    """The checks scrimmage test makes before its first run. It starts nothing."""
    try:
        config, problems = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("check", exc, found)
    stop_point()
    if config is None:
        if found is not None:
            found.messages += [(MISTAKES, problem) for problem in problems]
            return MISTAKE
        say(problems, sys.stderr)
        print(f"scrimmage check: {plural(len(problems), 'problem')}; nothing was run")
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
        f"scrimmage check: {path} is valid: {plural(len(config.tests), 'test')}, "
        f"{plural(rows, 'row')}, {plural(rows * agent.runs, 'run')}, {tools}, {budget}"
    )
    return OK


def init_command(args: argparse.Namespace, process: bool) -> int:
    folder, probe = args.folder, args.probe
    out = args.out if args.out is not None else folder / "scrimmage.toml"

    def run(path: Path, found: Found | None) -> int:
        return run_init(folder, path, probe, found)

    return as_json("init", out, run, process) if args.json else run(out, None)


def run_init(folder: Path, out: Path, probe: str | None, found: Found | None) -> int:
    """A draft from reading the code, and from a probe if asked. Never over an existing file."""
    from scrimmage_core import init as drafting
    from scrimmage_core.runner import Interrupted

    try:
        draft = drafting.prepare(
            folder,
            out,
            probe=probe,
            stop_flag=None if found is None else entry.pending,
            announce=announce if found is None else (lambda line: None),
        )
        stop_point()  # a signal that came during the read or the probe: nothing is written yet
        drafting.write(draft)
        if found is not None:
            settle()  # the file is written, so no signal may turn this into a failure now
    except Stopped:
        raise  # as_json says it, with the document
    except (Interrupted, KeyboardInterrupt) as exc:
        name = str(exc) or "SIGINT"
        return init_failed(
            f"stopped by {name}; every run was stopped, and no draft was written", found
        )
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the folder
        return init_failed(f"infrastructure error in scrimmage init: {exc!r}", found)
    if found is None:
        drafting.print_text(draft)
        return draft.code
    found.messages += draft.messages
    found.fields.update(draft.fields)
    return draft.code


def announce(line: str) -> None:
    print(line, flush=True)


def init_failed(message: str, found: Found | None) -> int:
    if found is not None:
        return found.fail(message)
    print(f"scrimmage: {message}", file=sys.stderr)
    return INFRA


if __name__ == "__main__":
    sys.exit(main(process=True))
