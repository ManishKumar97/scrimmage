"""The scrimmage command: scrimmage test, check, record, init, show, schema, cert and path.

With --json, standard output holds one JSON document and nothing else. Every command's
document starts with the same envelope; schemas/cli.v1.json describes it.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import math
import os
import re
import shlex
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast

from scrimmage_core import compare as comparing
from scrimmage_core import config as settings
from scrimmage_core import entry, redact
from scrimmage_core import plan as planner
from scrimmage_core import results as results_file
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
from scrimmage_core.render import plain

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
    "show": {OK: "shown", MISTAKE: "mistake", INFRA: "infra"},
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
    test.add_argument(
        "--records",
        type=Path,
        metavar="DIR",
        help="write the records folder inside DIR, not scrimmage-runs beside the file",
    )
    for command in (test, check, record):
        command.add_argument("path", nargs="?", default="scrimmage.toml", type=Path)
        command.add_argument("--json", action="store_true", help="print one JSON document")
    test.add_argument(
        "--compare",
        type=Path,
        metavar="EARLIER",
        help="compare this test with an earlier records folder or its result.json",
    )
    record.add_argument(
        "--test", action="append", default=[], metavar="NAME", help="record only this test"
    )
    record.add_argument(
        "--refresh", action="append", default=[], metavar="LABEL", help="record this label again"
    )
    record.add_argument("--runs", default="1", metavar="N", help="run each test's baseline N times")
    check.add_argument(
        "--runs", metavar="N", help="count the runs scrimmage record --runs N would start"
    )
    check.add_argument(
        "--service",
        action="store_true",
        help="also probe the service in the file and read its report; sends no run",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="with --service, a template that does not fit /openapi.json is exit 2",
    )
    for command in (test, record):
        command.add_argument(
            "--max-cost-usd",
            metavar="USD",
            help="lower the file's max_cost_usd to this for this command; it never raises it",
        )
    check.add_argument(
        "--max-cost-usd",
        metavar="USD",
        help="check --runs N under this cap, as record would run; budget_usd stays the file's",
    )
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
    show = commands.add_parser(
        "show", help="say what happened in a finished test", allow_abbrev=False
    )
    show.add_argument("target", nargs="?", type=Path, help="a records folder or its result.json")
    show.add_argument("--run", metavar="RUN", help="show only this run, such as run-3f9a1c40b2e1")
    show.add_argument(
        "--compare",
        type=Path,
        metavar="EARLIER",
        help="compare with an earlier records folder or its result.json",
    )
    show.add_argument("--json", action="store_true", help="print one JSON document")
    cert = commands.add_parser(
        "cert",
        help="make the certificates for a control server a container reaches",
        allow_abbrev=False,
    )
    cert.add_argument("--ip", action="append", default=[], metavar="ADDRESS", help="an IP SAN")
    cert.add_argument("--name", action="append", default=[], metavar="HOST", help="a DNS SAN")
    cert.add_argument("--out", required=True, type=Path, help="a new folder for the three files")
    cert.add_argument("--days", type=int, default=30, help="validity in days, 1 to 397; default 30")
    commands.add_parser(
        "path",
        help="print the folder scrimmage-run puts first on PYTHONPATH",
        allow_abbrev=False,
    )
    args = parser.parse_args(argv)
    if args.command == "check" and args.strict and not args.service:
        parser.error("--strict needs --service")
    if args.command == "cert":
        return cert_command(args)
    if args.command == "path":
        return path_command()
    if args.command == "init":
        return init_command(args, process)
    if args.command == "show":
        return show_command(args, process)
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
        compare, cap, records = args.compare, args.max_cost_usd, args.records

        def test(path: Path, found: Found | None) -> int:
            return run_test(path, found, compare, cap, records)

        return test
    if args.command == "check":
        record_runs, check_cap = args.runs, args.max_cost_usd

        def check(path: Path, found: Found | None) -> int:
            code = run_check(path, found, record_runs, check_cap, hint=not args.service)
            if code != OK or not args.service:
                return code
            return run_check_service(path, found, args.strict)

        return check
    names, refresh, runs = list(args.test), list(args.refresh), str(args.runs)
    record_cap = args.max_cost_usd

    def record(path: Path, found: Found | None) -> int:
        return run_record(path, found, names, refresh, runs, record_cap)

    return record


def settle() -> None:
    """From here on the command's code is settled: no signal may change the exit it states.

    A signal noted after the last stop point is dropped with it, so a later call in the
    same process does not start stopped.
    """
    if on_main_thread():
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
    entry.pending.clear()


def on_main_thread() -> bool:
    """Only the main thread can set a signal handler; from another thread none is set."""
    return threading.current_thread() is threading.main_thread()


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
    handled = on_main_thread()
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS} if handled else {}
    try:
        try:
            if handled:
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
        return importlib.metadata.version("scrimmage")
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
        "result_file": None,
        "compare": None,
    }


def empty_check() -> dict[str, object]:
    return {"summary": None}


def empty_record() -> dict[str, object]:
    return {
        "out": None,
        "records": None,
        "cost_usd": 0.0,
        "budget_usd": None,
        "stopped_by": None,
        "numbers": {"calls_seen": 0, "calls_written": 0},
        "tests": [],
    }


def empty_show() -> dict[str, object]:
    return {"records": None, "result": None, "runs": [], "compare": None}


def empty_init() -> dict[str, object]:
    from scrimmage_core import init as drafting  # init brings the runner, for its probe

    return drafting.empty_fields()


# A command's own fields, as they are when it stops before it has any to give.
EMPTY_FIELDS = {
    "test": empty_test,
    "check": empty_check,
    "record": empty_record,
    "init": empty_init,
    "show": empty_show,
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


# A dollar amount as Python's repr prints a float: digits, a point, an exponent. Not float()
# alone, which takes "nan", "inf", " 5" and full-width digits.
AMOUNT = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?", flags=re.ASCII)


def capped(config: settings.Config, cap: str | None) -> tuple[settings.Config | None, list[str]]:
    """The config with max_cost_usd lowered to --max-cost-usd, once, right after the file is
    read. Every reader takes the cap from the config, so the flag holds for all of them. The
    lower of the two wins, so the flag never raises a cap; a file with no budget takes it."""
    if cap is None:
        return config, []
    usd = float(cap) if AMOUNT.fullmatch(cap) else math.nan
    if not (math.isfinite(usd) and usd > 0):
        return None, [f"--max-cost-usd: expected a number of dollars above 0, got {cap!r}"]
    file_cap = config.agent.max_cost_usd
    lower = usd if file_cap is None else min(file_cap, usd)
    agent = dataclasses.replace(config.agent, max_cost_usd=lower)
    return dataclasses.replace(config, agent=agent), []


def say(problems: list[str], err: TextIO) -> None:
    for problem in problems:
        print(plain(f"scrimmage: {problem}"), file=err)


def unreadable(command: str, exc: Exception, found: Found | None) -> int:
    """Reading the file failed inside Scrimmage, as a parser that runs out of stack does."""
    message = f"infrastructure error in scrimmage {command}: {exc!r}"
    if found is not None:
        return found.fail(message)
    print(f"scrimmage: {message}", file=sys.stderr)
    last = "the file was not checked" if command == "check" else "the test did not start"
    print(f"scrimmage {command}: {last}; nothing was run")
    return INFRA


def run_test(
    path: Path,
    found: Found | None,
    compare: Path | None = None,
    cap: str | None = None,
    records: Path | None = None,
) -> int:
    from scrimmage_core import runner as running  # the heavy import waits until a test runs
    from scrimmage_core import service_client

    try:
        config, problems = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("test", exc, found)
    stop_point()
    if config is not None:
        config, problems = capped(config, cap)
    if config is None:
        return test_mistakes(problems, found)
    earlier: dict[str, object] | None = None
    if compare is not None:  # before any run, so a bad EARLIER costs nothing
        earlier, problems = read_earlier(compare)
        if earlier is None:
            return test_mistakes(problems, found)
    runner: Runner | None = None
    results: list[running.RunResult] = []
    stopped: str | None = None
    try:
        # With --json the runner shares the noted signal, so one that came just before its
        # runs start still stops them, and one during them is said once.
        runner = running.Runner(
            config, stop_flag=None if found is None else entry.pending, records=records
        )
        if found is not None:
            found.started(config, runner)
        stop_point()
        results = runner.run_all()
    except running.Interrupted as exc:
        stopped = f"stopped by {exc}; every run was stopped"
    except service_client.Setup as setup:
        # A service that is not ready: exit 2 for a mistake to fix, 3 when it is not gated.
        if runner is not None:
            runner.server.server.server_close()
        told = redact.remove_values(str(setup), config.scrub)[0]
        if found is None:
            print(f"scrimmage: {told}", file=sys.stderr)
            return setup.code
        found.messages.append((MISTAKES if setup.code == MISTAKE else INFRA_ERRORS, told))
        return setup.code
    except Exception as exc:  # the runner's own failure is never a result
        # A failure to start a run can quote its arguments, which can hold a value.
        failed = redact.remove_values(repr(exc), config.scrub)[0]
        stopped = f"infrastructure error in the runner: {failed}"
    finally:
        if runner is not None:
            runner.server.server.server_close()
    if stopped is not None:
        if found is None:
            print(f"scrimmage: {stopped}", file=sys.stderr)
            if compare is not None:
                print(f"scrimmage: {NOT_FINISHED}", file=sys.stderr)
            return INFRA
        if compare is not None:
            found.messages.append((NOTES, NOT_FINISHED))
        return found.fail(stopped)
    assert runner is not None
    report = running.build_report(config, results, runner.folder, runner)
    stop_point()  # a signal that came while the report was built: no file is written
    # The values ${NAME} filled in never reach the result file, the document or the report.
    for said in (report.above, report.messages):
        said[:] = [(kind, redact.remove_values(text, config.scrub)[0]) for kind, text in said]
    fields = cast(
        dict[str, object],
        redact.scrubbed({**running.report_json(report), "compare": None}, config.scrub),
    )
    version = scrimmage_version()
    doc = document("test", report.code, str(path), report.every_message, version, **fields)
    # A test the budget or a bad label stopped has rows with fewer runs than planned: it is not
    # a result to keep or to compare with.
    finished = report.stopped_by is None
    written, warning = save_result(runner.folder, doc) if finished else (None, None)
    if found is None:
        if warning is not None:
            print(f"scrimmage: warning: {warning}", file=sys.stderr)
        printed = io.StringIO()
        code = running.print_report(report, printed)
        sys.stdout.write(redact.remove_values(printed.getvalue(), config.scrub)[0])
        if compare is not None:
            say_comparison(earlier, doc, compare, finished, None)
        return code
    settle()  # the file is written, so no signal may change the code the document states
    found.messages += report.every_message
    if warning is not None:
        found.messages.append((WARNINGS, warning))
    found.fields.clear()  # the report's fields in their own order, so the file matches them
    found.fields.update(fields, result_file=None if written is None else str(written))
    if compare is not None:
        found.fields["compare"] = say_comparison(earlier, doc, compare, finished, found)
    return report.code


NOT_FINISHED = "the test did not finish, so it has no result to compare"


def test_mistakes(problems: list[str], found: Found | None) -> int:
    if found is None:
        say(problems, sys.stderr)
        print("scrimmage: the test stopped before the first run", file=sys.stderr)
    else:
        found.messages += [(MISTAKES, problem) for problem in problems]
    return MISTAKE


def read_earlier(path: Path) -> tuple[dict[str, object] | None, list[str]]:
    """The earlier result named by --compare, or why it cannot be compared with."""
    doc, problems = results_file.read(path)
    if doc is None:
        return None, problems
    problems = comparing.problems_with(doc, "earlier")
    return (None, problems) if problems else (doc, [])


def say_comparison(
    earlier: dict[str, object] | None,
    later: dict[str, object],
    path: Path,
    finished: bool,
    found: Found | None,
) -> dict[str, object] | None:
    """The comparison of two results: printed in the text form, returned for --json."""
    if earlier is None or not finished:
        if found is None:
            print(f"scrimmage: {NOT_FINISHED}", file=sys.stderr)
        else:
            found.messages.append((NOTES, NOT_FINISHED))
        return None
    try:
        compared = comparing.document(str(path), earlier, comparing.rows(earlier, later))
    except Exception as exc:  # the test ran and has its result: a comparison never changes that
        message = f"the comparison could not be made: {exc!r}"
        if found is None:
            print(f"scrimmage: warning: {message}", file=sys.stderr)
        else:
            found.messages.append((WARNINGS, message))
        return None
    if found is None:
        for line in comparing.text(compared):
            print(plain(line))
    return compared


def save_result(folder: Path, doc: dict[str, object]) -> tuple[Path | None, str | None]:
    """result.json in the records folder: the document --json prints, but for result_file.

    A write that fails is a warning and changes no exit code. Ctrl-C while it is written, in the
    text form, removes the half file and goes on up the stack.
    """
    try:
        return results_file.write(folder, doc), None
    except (
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else repr(exc)
        return None, f"{results_file.RESULT_FILE} was not written: {why}"


def run_record(
    path: Path,
    found: Found | None,
    names: list[str],
    refresh: list[str],
    runs: str = "1",
    cap: str | None = None,
) -> int:
    """One real baseline run per test, its real calls written as mocks beside the team's file."""
    from scrimmage_core import record as recording
    from scrimmage_core import runner as running

    try:
        config, problems = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("record", exc, found)
    stop_point()
    if config is not None:
        config, problems = capped(config, cap)
    if config is None:
        return record_mistakes(problems, found)
    plan, problems = recording.prepare(config, names, refresh, runs)
    if plan is None:
        return record_mistakes(problems, found)
    if (before := recording.before_runs(plan.config)) is not None:
        if found is None:
            print(f"scrimmage record: {before}", flush=True)
        else:  # standard error stays empty under --json
            found.messages.append((NOTES, before))
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
            found.fields["budget_usd"] = plan.config.agent.max_cost_usd
        stop_point()
        results = runner.run_all()
    except running.Interrupted as exc:
        stopped = f"stopped by {exc}; every run was stopped, and nothing was written"
    except Exception as exc:  # the runner's own failure is never a result
        failed = redact.remove_values(repr(exc), plan.config.scrub)[0]
        stopped = f"infrastructure error in the runner: {failed}"
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
        failed = redact.remove_values(repr(exc), plan.config.scrub)[0]  # it can quote a value
        message = f"infrastructure error while writing the file, nothing was written: {failed}"
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


def run_check(
    path: Path,
    found: Found | None,
    record_runs: str | None = None,
    cap: str | None = None,
    hint: bool = True,
) -> int:
    """The checks scrimmage test makes before its first run. It starts nothing.

    record_runs: --runs, so the runs counted are those scrimmage record --runs N would start.
    cap: --max-cost-usd, so --runs is checked under the cap that record would run with. The
    summary's budget_usd stays the file's own, which is what a caller compares a cap with.
    hint: say how to start a service, which scrimmage check --service has running already.
    """
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
    runs = rows * agent.runs
    under, problems = capped(config, cap)  # a bad --max-cost-usd is a mistake, with --runs or not
    if under is None or record_runs is not None:
        count = 1
        if under is not None and record_runs is not None:
            from scrimmage_core import record as recording  # only for --runs: it brings the runner

            count, problems = recording.runs_problems(under, record_runs)
        if problems:
            if found is not None:
                found.messages += [(MISTAKES, problem) for problem in problems]
                return MISTAKE
            say(problems, sys.stderr)
            print(f"scrimmage check: {plural(len(problems), 'problem')}; nothing was run")
            return MISTAKE
        runs = len(config.tests) * count  # one baseline per test, N times
    # As the file writes them: a value from the environment is never printed.
    entry_kind, entry_value = (
        (
            "command",
            agent.command_written if "${" in agent.command_written else shlex.join(agent.command),
        )
        if agent.command
        else ("service", str(settings.as_written(agent.service)))
        if agent.service
        else ("function", agent.function)
    )
    summary: dict[str, object] = {
        "tests": len(config.tests),
        "rows": rows,
        "runs": runs,
        "tools": len(agent.tools),
        "budget_usd": agent.max_cost_usd,
        "real": list(agent.real),
        "entry": {"kind": entry_kind, "value": entry_value},
        # The prices [agent.prices] sets; any other model takes the built-in table's.
        "prices": {
            model: {"input": r.input, "output": r.output, "cached_input": r.cached_input}
            for model, r in agent.prices
        },
    }
    if found is not None:
        found.fields["summary"] = summary
        return OK
    tools = plural(len(agent.tools), "tool") if agent.tools else "no tools list"
    budget = "no budget" if agent.max_cost_usd is None else f"a budget of ${agent.max_cost_usd:g}"
    print(
        f"scrimmage check: {path} is valid: {plural(len(config.tests), 'test')}, "
        f"{plural(rows, 'row')}, {plural(runs, 'run')}, {tools}, {budget}"
    )
    if agent.service and hint:
        print("start the service with these two set, with scrimmage-run and SCRIMMAGE_SERVICE=1:")
        for line in service_lines(agent):
            print(f"  {line}")
    return OK


def run_check_service(path: Path, found: Found | None, strict: bool) -> int:
    """scrimmage check --service, after the file passed: probe the service and say what it
    reported. Sends no run and no setup request."""
    from scrimmage_core import runner as running
    from scrimmage_core import service_check, service_client

    try:
        config, _ = read(path)
    except Exception as exc:  # Scrimmage's own failure, not a mistake in the file
        return unreadable("check", exc, found)
    assert config is not None  # run_check read the file and found it valid
    if not config.agent.service:
        message = "--service needs [agent] service in the file, and this file has none"
        if found is None:
            print(f"scrimmage: {message}", file=sys.stderr)
            return MISTAKE
        found.messages.append((MISTAKES, message))
        return MISTAKE
    stop_point()
    flag = None if found is None else entry.pending
    try:
        result = service_check.check(config, strict, flag)
    except running.Interrupted as exc:
        stopped = f"stopped by {exc}"
        if found is None:
            print(f"scrimmage: {stopped}", file=sys.stderr)
            return INFRA
        return found.fail(stopped)
    scrub = config.scrub
    if isinstance(result, service_client.Setup):
        told = redact.remove_values(str(result), scrub)[0]
        if found is None:
            print(f"scrimmage: {told}", file=sys.stderr)
            return result.code
        found.messages.append((MISTAKES if result.code == MISTAKE else INFRA_ERRORS, told))
        return result.code
    kinds = {
        service_check.WARN: WARNINGS,
        service_check.MISTAKE_LEVEL: MISTAKES,
        service_check.INFRA_LEVEL: INFRA_ERRORS,
    }
    if found is None:
        for line in result.lines:
            line.text = redact.remove_values(line.text, scrub)[0]
        service_check.print_lines(result.lines, sys.stdout)
        return result.code
    found.messages += [
        (kinds[level], redact.remove_values(t, scrub)[0]) for level, t in result.messages
    ]
    document = cast(dict[str, object], redact.scrubbed(result.document, scrub))
    found.messages += [
        (NOTES, note) for note in cast(dict[str, list[str]], document["template"])["notes"]
    ]
    found.fields["service"] = document
    return result.code


def service_lines(agent: settings.Agent) -> list[str]:
    """The two lines a service's environment needs, built from the file: the tools its gates
    swap, and the packages a plan may import from. The probe stops a test without them."""
    return [
        f"SCRIMMAGE_TOOLS={shlex.quote(planner.tools_text(agent.tools, agent.renames))}",
        f"SCRIMMAGE_ALLOW_IMPORTS={shlex.quote(','.join(agent.allowed_imports()))}",
    ]


def path_command() -> int:
    """scrimmage path: the folder scrimmage-run puts first on PYTHONPATH, on one line."""
    from scrimmage_core import run_command  # the folder is named once, where scrimmage-run is

    boot = Path(os.path.abspath(run_command.BOOT))
    if not (boot / "sitecustomize.py").is_file():
        print(
            f"scrimmage path: {boot} is not a folder on disk, so PYTHONPATH cannot name it; "
            "install scrimmage from its wheel",
            file=sys.stderr,
        )
        return MISTAKE
    print(boot)
    return OK


def cert_command(args: argparse.Namespace) -> int:
    """scrimmage cert: a throwaway authority and a server certificate, in a new folder."""
    from scrimmage_core import cert  # the one module that needs the optional extra

    out, ips, names = args.out, tuple(args.ip), tuple(args.name)
    try:
        files = cert.make_certificates(out.absolute(), ips=ips, names=names, days=args.days)
    except cert.CertError as exc:
        print(f"scrimmage: {exc}", file=sys.stderr)
        return MISTAKE
    host = names[0] if names else ips[0]
    shown = f"[{host}]" if ":" in host else host
    valid = plural(args.days, "day")
    print(f"scrimmage cert: wrote {files.ca}, {files.cert} and {files.key}, valid {valid}")
    print(
        "the service's environment (in a container, SCRIMMAGE_CONTROL_CA is the path where "
        "ca.pem is mounted there):"
    )
    print(f"  SCRIMMAGE_CONTROL_URL=https://{shown}:<port>")
    print(f"  SCRIMMAGE_CONTROL_CA={files.ca}")
    print("the [agent] table of scrimmage.toml (absolute paths, so it works from any folder):")
    print(f'  control_tls = {{cert = "{files.cert}", key = "{files.key}"}}')
    return OK


def init_command(args: argparse.Namespace, process: bool) -> int:
    folder, probe = args.folder, args.probe
    out = args.out if args.out is not None else folder / "scrimmage.toml"

    def run(path: Path, found: Found | None) -> int:
        return run_init(folder, path, probe, found)

    return as_json("init", out, run, process) if args.json else run(out, None)


def show_command(args: argparse.Namespace, process: bool) -> int:
    target, run_id, compare = args.target, args.run, args.compare

    def run(path: Path, found: Found | None) -> int:
        del path
        return run_show(target, run_id, compare, found)

    shown_path = target if target is not None else Path()
    return as_json("show", shown_path, run, process) if args.json else run(shown_path, None)


def run_show(
    target: Path | None, run_id: str | None, compare: Path | None, found: Found | None
) -> int:
    """What happened in a finished test, read from its records folder. It runs nothing."""
    from scrimmage_core import show as showing

    shown = showing.build(target, run_id, Path.cwd())
    if found is not None and target is None and shown.records is not None:
        found.path = str(shown.records)  # config says the folder chosen
    compared: dict[str, object] | None = None
    if compare is not None and not shown.mistakes:
        earlier, problems = read_earlier(compare)
        if earlier is None:
            shown.mistakes += problems
        elif shown.result is None:
            shown.mistakes.append(f"{shown.records}: has no {results_file.RESULT_FILE} to compare")
        else:
            compared = comparing.document(
                str(compare), earlier, comparing.rows(earlier, shown.result)
            )
    if shown.mistakes:
        shown.code = MISTAKE
    if found is not None:
        found.fields.update(shown.fields, compare=compared)
        found.messages += [(MISTAKES, m) for m in shown.mistakes]
        found.messages += [(NOTES, n) for n in shown.notes]
        return shown.code
    if shown.mistakes:
        say(shown.mistakes, sys.stderr)
        return shown.code
    for line in shown.text:
        print(plain(line))
    for note in shown.notes:
        print(plain(f"note: {note}"))
    if compared is not None:
        for line in comparing.text(compared):
            print(plain(line))
    return shown.code


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
