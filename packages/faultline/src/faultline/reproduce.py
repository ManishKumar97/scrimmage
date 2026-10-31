"""`faultline test`: run each task several times clean and several times under a fault.

    faultline test --tasks FILE --fault SPEC [--fault SPEC]... [--runs N]
                   [--clean-runs N] [--out DIR] [--expect-from FILE] -- CMD...

The tasks file is TOML, one table per task:

    [[task]]
    id = "cart-fails"
    input = "Customers report the cart fails. Why?"
    expected = "cart"   # optional

`{input}` in CMD becomes the task's input. A CMD without it gets the input as its
last argument. Every run goes through the same launcher as `faultline run`, one at
a time: first a task's clean runs, then its runs under each fault in turn. Each run
is saved in its own directory:

    DIR/<task id>/clean/<n>/                 a clean run
    DIR/<task id>/<fault, filesystem-safe>/<n>/  a run under that fault

Each holds `trace/` (the run's spans), `stdout.txt`, `stderr.txt` and `result.json`.
At the end, the fault matrix is printed and written to DIR/matrix.json.

Cost. After the first run, before the rest start, it prints an estimate: that run's
tokens times every run, priced from the config's [cost] section or from --price-in
and --price-out. `--max-cost USD` stops starting runs once they cost that much, and
exits 3. Mid-run, the proxy refuses model calls past the cap with HTTP 402.

Live tools. Without --allow-live-tools, no run is made: the agent starts once on
the first task as a preview, as `faultline run` does, its tools are listed, and
faultline test exits 2. Reproduce reruns the agent many times, and no rerun may
reach a live tool unless the team says so.

Each run gets a verdict by the rules in `faultline.verdict`. A task's reference is
its expected answer, else the answer of its first clean run that got past rule 1.
`--matcher module:function` replaces the built-in comparison with the team's own.
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from faultline import matrix
from faultline.config import CostConfig, load_config
from faultline.cost import (
    COST_CAP,
    COST_UNKNOWN,
    NO_USAGE,
    Budget,
    Prices,
    dollars,
    tokens,
    unmetered,
)
from faultline.faults import Fault, FaultError, parse_all
from faultline.launch import (
    PREVIEW,
    Outcome,
    Streams,
    add_live_tools_argument,
    add_upstream_arguments,
    command_of,
    execute,
    proxy_ready,
    routed_upstreams,
    show_tools,
)
from faultline.model import RunVerdict
from faultline.verdict import (
    Matcher,
    Reference,
    compare,
    expected,
    judge,
    loud,
    not_applied_line,
)

CLEAN = "clean"
INPUT = "{input}"
TASK_KEYS = ("id", "input", "expected")
# A task id is a directory name, so only these characters, and not "." or "..".
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
UNSAFE = re.compile(r"[^A-Za-z0-9._=-]")
# The exit code after a stop signal: what a shell gives for Ctrl-C.
INTERRUPTED = 130
# The verdict of the clean run whose answer is its task's reference: it was
# compared with nothing, so it counts as no verdict.
REFERENCE_RUN = "no verdict: reference run"
# The verdict of a run the proxy cut short at the cost cap.
STOPPED_BY_CAP = "no verdict: stopped by --max-cost"
# The exit code when --max-cost stopped the test before its last run.
CAP_EXIT = 3


class TasksError(ValueError):
    """A tasks file faultline cannot use. The message names the problem."""


@dataclass(frozen=True)
class Task:
    id: str
    input: str
    expected: str | None = None  # the team's expected answer, if it gave one


def parse_tasks(text: str, need_input: bool = True) -> list[Task]:
    """The tasks in a TOML text. Raises TasksError naming what is wrong."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TasksError(f"not valid TOML: {exc}") from None
    unknown = sorted(set(data) - {"task"})
    if unknown:
        raise TasksError(f"unknown table {unknown[0]}; write each task as [[task]]")
    tables = data.get("task", [])
    if not isinstance(tables, list) or not tables:
        raise TasksError("no tasks; write each task as [[task]]")
    tasks: list[Task] = []
    for number, table in enumerate(tables, start=1):
        extra = sorted(set(table) - set(TASK_KEYS))
        if extra:
            raise TasksError(
                f"task {number}: unknown key {extra[0]}; known: {', '.join(TASK_KEYS)}"
            )
        for key in TASK_KEYS:
            if key in table and not isinstance(table[key], str):
                raise TasksError(f"task {number}: {key} must be a string")
        task_id = table.get("id", "")
        if not TASK_ID.fullmatch(task_id):
            raise TasksError(
                f"task {number}: id {task_id!r} must be letters, digits, '.', '_' "
                "or '-', starting with a letter or digit"
            )
        if need_input and "input" not in table:
            raise TasksError(f"task {task_id}: no input")
        tasks.append(Task(task_id, table.get("input", ""), table.get("expected")))
    ids = [task.id for task in tasks]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise TasksError(f"task id {repeated[0]} is used twice")
    # A disk that ignores case, as macOS's does, would put both in one directory.
    folded: dict[str, str] = {}
    for task_id in ids:
        other = folded.setdefault(task_id.lower(), task_id)
        if other != task_id:
            raise TasksError(f"task ids {other} and {task_id} differ only in case")
    return tasks


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TasksError(f"cannot read {path}: {exc}") from None


def load_tasks(path: Path, expect_from: Path | None = None) -> list[Task]:
    """The tasks in a file. An expected answer in `expect_from`, a file of tasks with
    id and expected only, wins over the one in the tasks file."""
    try:
        tasks = parse_tasks(read_text(path))
    except TasksError as exc:
        raise TasksError(f"{path}: {exc}") from None
    if expect_from is None:
        return tasks
    try:
        given = parse_tasks(read_text(expect_from), need_input=False)
    except TasksError as exc:
        raise TasksError(f"{expect_from}: {exc}") from None
    expected = {task.id: task.expected for task in given if task.expected is not None}
    ids = {task.id for task in tasks}
    stray = sorted(expected.keys() - ids)
    if stray:
        raise TasksError(f"{expect_from}: no task {stray[0]} in {path}")
    return [
        Task(task.id, task.input, expected.get(task.id, task.expected))
        for task in tasks
    ]


def command_for(command: Sequence[str], text: str) -> list[str]:
    """The command with {input} replaced, or with the input appended."""
    if any(INPUT in part for part in command):
        return [part.replace(INPUT, text) for part in command]
    return [*command, text]


def safe_names(specs: Sequence[str]) -> dict[str, str]:
    """A directory name for each fault spec. Two specs that clean up to the same name
    get a short hash each, so no two specs share a directory."""
    names = {spec: UNSAFE.sub("_", spec) for spec in specs}
    counts = list(names.values())
    for spec, name in names.items():
        if counts.count(name) > 1:
            digest = hashlib.sha256(spec.encode()).hexdigest()[:8]
            names[spec] = f"{name}-{digest}"
    return names


@dataclass(frozen=True)
class Planned:
    """One run to make."""

    task: Task
    fault: Fault | None  # None for a clean run
    number: int  # from 1, within the task and fault
    directory: Path


def plan(
    tasks: Sequence[Task],
    faults: Sequence[Fault],
    clean_runs: int,
    runs: int,
    out: Path,
) -> list[Planned]:
    """Every run, in the order they are made: a task's clean runs, then its runs
    under each fault."""
    names = safe_names([fault.spec for fault in faults])
    planned = []
    for task in tasks:
        for number in range(1, clean_runs + 1):
            planned.append(
                Planned(task, None, number, out / task.id / CLEAN / str(number))
            )
        for fault in faults:
            folder = out / task.id / names[fault.spec]
            for number in range(1, runs + 1):
                planned.append(Planned(task, fault, number, folder / str(number)))
    return planned


@dataclass(frozen=True)
class Result:
    """A run made, and its verdict or why it has none."""

    planned: Planned
    outcome: Outcome
    verdict: RunVerdict | str
    reference: Reference | None = None


class References:
    """Each task's reference: its expected answer, else the answer of its first clean
    run that got past rule 1 of the verdict."""

    def __init__(self, matcher: Matcher | None = None) -> None:
        self.found: dict[str, Reference] = {}
        self.runs: dict[str, int] = {}  # the clean run each reference came from
        self.disagree: set[str] = set()  # tasks whose clean runs differ
        self.matcher = matcher

    def of(self, task: Task) -> Reference | None:
        if task.expected is not None:
            return expected(task.expected)
        found = self.found.get(task.id)
        if found is None:
            return None
        return replace(found, agreed=task.id not in self.disagree)

    def is_reference(self, planned: Planned) -> bool:
        return planned.fault is None and self.runs.get(planned.task.id) == (
            planned.number
        )

    def learn(self, planned: Planned, outcome: Outcome) -> None:
        """Take a clean run's answer when its task has no reference yet. Note a
        later clean run that does not match it."""
        task, run = planned.task, outcome.run
        if planned.fault or task.expected is not None or run is None:
            return
        if not run.answer or loud(run, outcome.code) is not None:
            return  # a clean run that failed loudly is not judged
        found = self.found.get(task.id)
        if found is None:
            source = f"clean run {planned.number}"
            self.found[task.id] = Reference(run.answer, False, source)
            self.runs[task.id] = planned.number
            return
        try:
            same, _ = compare(run.answer, found.text, self.matcher)
        except Exception:  # noqa: BLE001 - the matcher is the team's code
            same = False  # the run's own verdict says the matcher raised
        if not same:
            self.disagree.add(task.id)


def verdict_of(
    outcome: Outcome, reference: Reference | None, matcher: Matcher | None = None
) -> RunVerdict | str:
    if outcome.missed:
        return not_applied_line([spec for spec, _ in outcome.missed])
    steps = outcome.run.steps if outcome.run else ()
    if any(step.error_type in (COST_CAP, COST_UNKNOWN) for step in steps):
        # The proxy refused a model call: faultline ended the run, not the agent.
        return STOPPED_BY_CAP
    return judge(
        outcome.run,
        outcome.trace_id,
        outcome.code,
        reference,
        outcome.answer_unrecorded,
        matcher,
    )


def load_matcher(spec: str) -> Matcher:
    """The team's function from module:function. The working directory is importable,
    so a matcher can live beside the tasks file. Raises ValueError saying why not."""
    module_name, _, name = spec.partition(":")
    if not module_name or not name:
        raise ValueError(f"--matcher {spec}: write it as module:function")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(
            f"--matcher {spec}: cannot import {module_name}: {exc}"
        ) from None
    function = getattr(module, name, None)
    if callable(function):
        return function
    raise ValueError(f"--matcher {spec}: {module_name} has no function {name}")


def result_json(result: Result) -> dict:
    planned, outcome, verdict = result.planned, result.outcome, result.verdict
    judged = isinstance(verdict, RunVerdict)
    return {
        "task": planned.task.id,
        "fault": planned.fault.spec if planned.fault else None,
        "number": planned.number,
        "exit_code": outcome.code,
        "seconds": round(outcome.seconds, 3),
        "answer": outcome.run.answer if outcome.run else None,
        "verdict": verdict.verdict.value if judged else None,
        "reason": verdict.reason if judged else verdict,
        "reference": result.reference.source if result.reference else None,
        "trace_id": outcome.trace_id,
    }


def make(
    planned: Planned,
    command: Sequence[str],
    upstreams: dict[str, str | None],
    max_content: int,
    budget: Budget | None = None,
) -> Outcome:
    """Make one run and save its output in its directory."""
    planned.directory.mkdir(parents=True)
    faults = [planned.fault] if planned.fault else []
    with (
        (planned.directory / "stdout.txt").open("wb") as stdout,
        (planned.directory / "stderr.txt").open("w", encoding="utf-8") as stderr,
    ):
        streams = Streams(subprocess.DEVNULL, stdout, stderr, log=stderr)
        return execute(
            command_for(command, planned.task.input),
            faults,
            upstreams,
            planned.directory / "trace",
            max_content,
            streams=streams,
            budget=budget,
        )


def prices_of(args: argparse.Namespace, config: CostConfig) -> Prices | None:
    """The prices from the flags, else from the config. None when not given.
    Raises ValueError when only one of the two is known."""
    price_in = args.price_in if args.price_in is not None else config.input_per_mtok
    price_out = args.price_out if args.price_out is not None else config.output_per_mtok
    if price_in is None and price_out is None:
        return None
    if price_in is None or price_out is None:
        raise ValueError(
            "give both prices: --price-in and --price-out, or [cost] "
            "input_per_mtok and output_per_mtok"
        )
    if price_in < 0 or price_out < 0:
        raise ValueError("a price cannot be negative")
    return Prices(price_in, price_out)


def plan_line(
    tasks: int,
    faults: int,
    clean_runs: int,
    runs: int,
    first: tuple[int, int],
    prices: Prices | None,
) -> str:
    """The plan and its estimate: the first clean run's tokens times every run.

    plan: 2 tasks x (3 clean + 3 faulted) = 12 runs, about 48,000 tokens, about $0.19
    """
    total = tasks * (clean_runs + runs * faults)
    faulted = f"{runs} faulted" if faults == 1 else f"{runs} faulted x {faults} faults"
    head = (
        f"plan: {tasks} task{'s' if tasks != 1 else ''} x "
        f"({clean_runs} clean + {faulted}) = {total} runs"
    )
    if sum(first) == 0:
        return f"{head}, tokens unknown (the first run recorded no token usage)"
    head += f", about {sum(first) * total:,} tokens"
    if prices is None:
        return (
            f"{head}, cost unknown (no price: set [cost] input_per_mtok and "
            "output_per_mtok, or pass --price-in and --price-out)"
        )
    return f"{head}, about {dollars(prices.cost(*first) * total)}"


def label(planned: Planned) -> str:
    where = planned.fault.spec if planned.fault else CLEAN
    return f"{planned.task.id} {where} #{planned.number}"


def describe(verdict: RunVerdict | str) -> str:
    if isinstance(verdict, str):
        return verdict
    return f"{verdict.verdict.value} ({verdict.reason})"


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "test",
        help="run each task several times clean and under each fault",
        description="Run CMD on each task, clean and under each fault, and save "
        "every run.",
    )
    parser.add_argument("--tasks", type=Path, required=True, metavar="FILE")
    parser.add_argument(
        "--fault",
        action="append",
        required=True,
        metavar="SPEC",
        help="a fault to test under (repeatable); each run has one fault",
    )
    parser.add_argument(
        "--runs", type=int, default=3, metavar="N", help="runs per task and fault"
    )
    parser.add_argument(
        "--clean-runs", type=int, default=3, metavar="N", help="clean runs per task"
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="where the runs are saved (default: a new temporary directory)",
    )
    parser.add_argument(
        "--expect-from",
        type=Path,
        metavar="FILE",
        help="expected answers, as [[task]] tables with id and expected; "
        "they win over the tasks file's",
    )
    parser.add_argument(
        "--matcher",
        metavar="MODULE:FUNCTION",
        help="your own comparison, called as FUNCTION(answer, reference) and "
        "returning True when they say the same",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="TOML settings, default ./faultline.toml if present; prices are in [cost]",
    )
    parser.add_argument(
        "--price-in",
        type=float,
        metavar="USD",
        help="US dollars per million input tokens (wins over [cost] input_per_mtok)",
    )
    parser.add_argument(
        "--price-out",
        type=float,
        metavar="USD",
        help="US dollars per million output tokens (wins over [cost] output_per_mtok)",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        metavar="USD",
        help="stop once the runs have cost this much; needs the prices. The proxy "
        "also refuses model calls past it, mid-run. The cap can be passed by one "
        "model call: a call's cost is known only once it is answered",
    )
    add_upstream_arguments(parser)
    add_live_tools_argument(parser)
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- CMD...")
    parser.set_defaults(handler=test)


def fail(message: str) -> int:
    print(f"faultline test: {message}", file=sys.stderr)
    return 2


def test(args: argparse.Namespace) -> int:
    command = command_of(args)
    if not command:
        return fail("give the agent's command after --")
    if args.runs < 1 or args.clean_runs < 1:
        return fail("--runs and --clean-runs must be 1 or more")
    try:
        faults = parse_all(args.fault)
        tasks = load_tasks(args.tasks, args.expect_from)
        matcher = load_matcher(args.matcher) if args.matcher else None
        prices = prices_of(args, load_config(args.config).cost)
    except (FaultError, TasksError, ValueError) as exc:  # ConfigError too
        return fail(str(exc))
    if args.max_cost is not None and prices is None:
        return fail(
            "--max-cost needs the prices: --price-in and --price-out, or [cost] "
            "in the config"
        )
    if args.max_cost is not None and args.max_cost <= 0:
        return fail("--max-cost must be above 0")
    if len(set(args.fault)) != len(args.fault):
        twice = sorted({spec for spec in args.fault if args.fault.count(spec) > 1})
        return fail(f"--fault {twice[0]} is given twice")
    if args.out and args.out.is_symlink():  # a dangling link too
        return fail(f"--out {args.out} is a symbolic link; give a real directory")
    used = args.out and args.out.exists()
    if used and (not args.out.is_dir() or any(args.out.iterdir())):
        return fail(f"{args.out} is not an empty directory; runs are never mixed")
    upstreams = routed_upstreams(args, "faultline test")
    if upstreams is None or not proxy_ready("faultline test"):
        return 2
    if not args.allow_live_tools:
        return preview(command, tasks[0], upstreams, args.max_content)
    out = args.out or Path(tempfile.mkdtemp(prefix="faultline-test-"))
    planned = plan(tasks, faults, args.clean_runs, args.runs, out)
    print(f"faultline test: {len(planned)} runs, saved in {out}", file=sys.stderr)
    references = References(matcher)
    results: list[Result] = []
    spent = 0.0  # what the runs so far cost, from their traces
    try:
        for index, run in enumerate(planned, start=1):
            budget = (
                Budget(args.max_cost, prices, spent)
                if args.max_cost is not None and prices
                else None
            )
            outcome = make(run, command, upstreams, args.max_content, budget)
            used = tokens(outcome.run)
            spent += prices.cost(*used) if prices else 0.0
            references.learn(run, outcome)
            reference = references.of(run.task)
            verdict = (
                REFERENCE_RUN
                if references.is_reference(run) and not outcome.missed
                else verdict_of(outcome, reference, matcher)
            )
            result = Result(run, outcome, verdict, reference)
            results.append(result)
            (run.directory / "result.json").write_text(
                json.dumps(result_json(result), indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"[{index}/{len(planned)}] {label(run)}: {describe(result.verdict)}",
                file=sys.stderr,
            )
            if index == 1:  # the estimate, before the rest start
                line = plan_line(
                    len(tasks), len(faults), args.clean_runs, args.runs, used, prices
                )
                print(line, file=sys.stderr)
            if outcome.interrupted:
                print(
                    f"faultline test: stopped by a signal after {index} of "
                    f"{len(planned)} runs",
                    file=sys.stderr,
                )
                report(results, faults, out)
                return INTERRUPTED
            cap: float | None = args.max_cost
            if cap is not None and unmetered(outcome.run):
                # Fail closed: a cost that cannot be counted cannot be capped.
                print(
                    f"faultline test: {NO_USAGE}; stopped after {index} of "
                    f"{len(planned)} runs",
                    file=sys.stderr,
                )
                report(results, faults, out)
                return CAP_EXIT
            if cap is not None and spent >= cap and index < len(planned):
                print(
                    f"faultline test: stopped after {index} of {len(planned)} runs: "
                    f"they cost about {dollars(spent)}, and the --max-cost cap is "
                    f"{dollars(cap)}",
                    file=sys.stderr,
                )
                report(results, faults, out)
                return CAP_EXIT
    except KeyboardInterrupt:
        # Ctrl-C between runs, when no run holds the signal: keep what was made.
        print(
            f"faultline test: stopped by Ctrl-C after {len(results)} of "
            f"{len(planned)} runs",
            file=sys.stderr,
        )
        report(results, faults, out)
        return INTERRUPTED
    report(results, faults, out)
    return 0


def preview(
    command: Sequence[str],
    task: Task,
    upstreams: dict[str, str | None],
    max_content: int,
) -> int:
    """Start the agent on the first task with every tool blocked, list the tools
    that would run for real, and make no run. Exits 2."""
    trace = Path(tempfile.mkdtemp(prefix="faultline-preview-"))
    # The agent's output goes to stderr, so stdout stays for the matrix.
    streams = Streams(subprocess.DEVNULL, sys.stderr.fileno())
    outcome = execute(
        command_for(command, task.input),
        [],
        upstreams,
        trace,
        max_content,
        streams=streams,
        preview=True,
    )
    print(
        show_tools(outcome.servers, "faultline test", outcome.unrouted),
        file=sys.stderr,
    )
    return PREVIEW


def report(results: Sequence[Result], faults: Sequence[Fault], out: Path) -> None:
    """Write DIR/matrix.json and print the matrix."""
    marks = [
        matrix.Mark(
            result.planned.fault.spec if result.planned.fault else matrix.CLEAN,
            result.verdict.verdict if isinstance(result.verdict, RunVerdict) else None,
            str(result.planned.directory.relative_to(out)),
        )
        for result in results
    ]
    table = matrix.rows(marks, [fault.spec for fault in faults])
    (out / "matrix.json").write_text(
        json.dumps(matrix.to_json(table), indent=2) + "\n", encoding="utf-8"
    )
    print(matrix.show(table))
    print(f"faultline test: runs and matrix.json in {out}", file=sys.stderr)
