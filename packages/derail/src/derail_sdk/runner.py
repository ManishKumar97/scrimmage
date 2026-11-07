"""The runner: one new program per run, the run table, exit codes.

Exit codes: 0 when every row passed; 1 when a row failed; 2 for a mistake in the test, a
refused call and a test the budget stopped included; 3 for an infrastructure error. The
higher code wins, as a mistake or an error makes the rates not worth trusting.
"""

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from derail_sdk import judge, labels
from derail_sdk import plan as planner
from derail_sdk.budget import Ledger
from derail_sdk.config import QUESTION, Config, Fault, Test
from derail_sdk.control import ControlServer, proc_key
from derail_sdk.records import RECORD_VERSION, RecordStore
from derail_sdk.redact import redact
from derail_sdk.state import COMMAND_CAP, now_iso

OK, ROW_FAILED, MISTAKE, INFRA = 0, 1, 2, 3
TAIL_BYTES = 1024 * 1024  # how much of a script's output is read to find its last line
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
KILL_GRACE_S = 5.0
GROUP_POLL_S = 0.01
OUTCOMES = (
    "answered",
    "crashed",
    "timed out",
    "refused call",
    "budget reached",
    "budget held",
    "infrastructure error",
    "stopped",
)
# A refusal for one of these reasons stops the whole test, not only its row.
STOP_TEST = (
    "budget",
    "budget_held",
    "no_price",
    "unknown_tool_type",
    "unseen_tool",
    "structure",
    "ambiguous_label",
)
# The agent's own cancellation is not a failure of the provider.
NOT_A_FAILURE = (
    "CancelledError",
    "KeyboardInterrupt",
    "SystemExit",
    "GeneratorExit",
    "DerailStopped",
)
# The agent chose this call. Under a fault that is what it did about the failure.
CHOSEN_CALLS = ("unlisted", "web_write", "blind_child")
PROCESS_LABEL = "process:python"
# A baseline whose every run ended so cannot be compared with, so its faults are skipped.
FAILED = ("crashed", "timed out", "refused call", "budget reached", "budget held")
TABLE_WIDTH = 120
SHOWN_RUNS = 3  # how many runs a line names for one broken rule
SHOWN_NAMES = 5  # how many model names a line lists
HEAD = [
    "test",
    "row",
    "fault",
    "runs",
    "fired",
    "done",
    "passed",
    "rate",
    "range",
    "tools",
    "cost",
    "result",
]
TEST_COLUMN, FAULT_COLUMN = 0, 2
NOT_JUDGED = judge.RunJudgement(left_out="not judged")


@dataclass
class RunResult:
    run: str
    row: planner.Row
    outcome: str
    answer: str | None
    fault_fired: bool | None
    calls: dict[str, int]
    mistakes: list[str] = field(default_factory=list[str])
    model_calls: int = 0
    cost_usd: float = 0.0
    notices: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    real_errors: list[str] = field(default_factory=list[str])
    hosted_reads: int = 0
    mocked: set[int] = field(default_factory=set[int])  # the mocks this run used, by place
    tools_seen: list[str] | None = None  # None: the run did not report them
    near_mocks: set[str] = field(default_factory=set[str])  # real, though the host had mocks
    tool_calls: int = 0
    models: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])  # label, name
    budget: dict[str, object] | None = None  # the numbers of a refusal for the budget
    judgement: judge.RunJudgement = NOT_JUDGED

    @property
    def exercised(self) -> bool:
        return self.fault_fired is not False or self.outcome == "stopped"


class Runner:
    def __init__(self, config: Config, control_url: Callable[[str], str] = lambda url: url) -> None:
        self.config = config
        self.control_url = control_url  # lets a test point the agent elsewhere
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        runs = config.root / "derail-runs"
        self.folder = runs / f"{stamp}-{secrets.token_hex(3)}"
        self.store = RecordStore(self.folder)
        # Records can hold customer data, so they stay out of the team's git repo.
        ignore = runs / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        self.token = secrets.token_hex(32)
        self.ledger = Ledger(config.agent.max_cost_usd)
        self.server = ControlServer(
            self.token, self.store, on_refused=self._refused, ledger=self.ledger
        )
        self.answers = Path(tempfile.mkdtemp(prefix="derail-answers-"))
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.stopped_rows: set[tuple[str, str]] = set()
        self.row_of: dict[str, planner.Row] = {}
        self.cancelled = False
        self.tools_seen: list[str] | None = None  # as the agent's process reported them
        self.label_problems: list[str] = []
        self.stopped_by: str | None = None  # the reason a refusal stopped the whole test
        self.skipped: list[str] = []  # tests whose fault rows did not run: baseline failed

    def _refused(self, run: str, reason: str) -> None:
        """The first refusal ends its run and its row; some reasons end the whole test."""
        with self.lock:
            row = self.row_of[run]
            self.stopped_rows.add((row.test.name, row.name))
            proc = self.procs.get(run)
            stop_test = reason in STOP_TEST and self.stopped_by is None
            if stop_test:
                self.stopped_by = reason
                self.cancelled = True  # under this lock, so no run starts from here on
        if proc is not None:
            stop_group(proc)
        if stop_test:
            self.cancel()

    def env(self, run: str) -> dict[str, str]:
        agent = self.config.agent
        env = dict(os.environ)
        for name in ("DERAIL_ROOT_PID", "DERAIL_PYTHON", "DERAIL_CHILDREN"):
            env.pop(name, None)  # the program sets the first two for itself
        env.update(
            DERAIL_TOKEN=self.token,
            DERAIL_CONTROL_URL=self.control_url(self.server.url),
            DERAIL_RUN=run,
            DERAIL_ANSWER_FILE=str(self.answers / f"{run}.txt"),
            DERAIL_ALLOW_IMPORTS=",".join(agent.allowed_imports()),
            DERAIL_TOOLS=planner.tools_text(agent.tools, agent.renames),
        )
        if agent.children:
            env["DERAIL_CHILDREN"] = "1"
        return env

    def program(self, question: str) -> list[str]:
        """What one run starts. A script's command was split when the file was read, so the
        question stays one argument, and no shell ever sees it."""
        agent = self.config.agent
        if agent.command:
            return [part.replace(QUESTION, question) for part in agent.command]
        answer = [agent.answer] if agent.answer else []
        return [sys.executable, "-m", "derail_sdk.start", agent.function, question, *answer]

    def one(self, row: planner.Row) -> RunResult | None:
        with self.lock:
            if self.cancelled or (row.test.name, row.name) in self.stopped_rows:
                return None
            run = f"run-{secrets.token_hex(6)}"
            self.row_of[run] = row
        plan = planner.plan_for(self.config, row, run)
        self.server.open_run(run, row.name, plan.to_json())
        if self.cancelled:
            self.server.close_run(run)
            return None
        command = [sys.executable, "-m", "derail_sdk.run_command", *self.program(row.test.question)]
        # A script's last printed line can be its answer, so what it prints stays apart.
        log = owner_only(self.folder / f"{run}.log")
        out = owner_only(self.folder / f"{run}.out") if self.config.agent.command else log
        try:
            proc = subprocess.Popen(
                command,
                cwd=self.config.root,
                env=self.env(run),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=log,
                start_new_session=True,  # its own process group, so every child stops too
            )
        finally:
            os.close(log)
            if out != log:
                os.close(out)
        with self.lock:
            self.procs[run] = proc
        # Refused, or the test was stopped, before this program was registered.
        if self.server.runs[run].refused or self.cancelled:
            stop_group(proc)
        timed_out = False
        try:
            proc.wait(timeout=self.config.agent.run_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        stop_group(proc)  # at the time limit, or a child that outlived the program
        left = kill_those_who_left(run, proc.pid)
        with self.lock:
            del self.procs[run]
        state = self.server.close_run(run)
        kept_out = self.other_python_lines(run, row)
        # The server stops a test on a thread of its own, which this run's end can overtake.
        # Settle it here, so the next run sees it.
        if state.broken or state.refusals or kept_out:
            reason = "other_python" if kept_out else (state.refusals or ["structure"])[0]
            self._refused(run, "structure" if state.broken else reason)
        lines = self.store.lines(run)
        answer = self.answer_of(run, proc.returncode)
        # Another run stopped the test and this run was stopped with it. That says
        # nothing about this run, and it never got to send its last batch.
        stopped = (
            self.cancelled and not timed_out and not state.refused and not state.broken
        ) and proc.returncode != 0
        ended_itself = not timed_out and not state.refused and not stopped
        # Every process that ended by itself sent a last batch saying how many lines it
        # made. A missing batch, or fewer lines received from a process, is a gap.
        per_proc = Counter(proc_key(line.get("proc")) for line in lines)
        gap = any(per_proc[proc] < made for proc, made in state.made.items())
        unfinished = not state.made or any(proc not in state.made for proc in per_proc)
        incomplete = bool(state.dropped) or (ended_itself and (gap or unfinished))
        made = kept_out + [self.left_line(run, row, *entry) for entry in left]
        self.store.add(run, made)
        lines += made
        model = [line for line in lines if line.get("kind") == "model"]
        notices = [line for line in lines if "notice" in line]
        cost = sum(float(str(line.get("cost_usd") or 0)) for line in model)
        # The provider failing by itself says nothing about the agent.
        real_errors = [e for e in map(real_error, model) if e is not None]
        broken = any(n.get("notice") == "structure" for n in notices)
        no_plan = "no_plan" in state.refusals  # says nothing about the agent
        if stopped:
            outcome = "stopped"
        elif not state.fetched or incomplete or real_errors or broken or no_plan:
            outcome = "infrastructure error"
        elif state.refusals[:1] == ["budget"]:
            outcome = "budget reached"
        elif state.refusals[:1] == ["budget_held"]:
            outcome = "budget held"
        elif state.refused or kept_out:
            outcome = "refused call"
        elif timed_out:
            outcome = "timed out"
        elif proc.returncode == 0 and answer is not None:
            outcome = "answered"
        else:
            outcome = "crashed"
        calls = {
            a: sum(1 for line in lines if line.get("action") == a)
            for a in ("real", "mock", "fault", "refused")
        }
        fired = None if row.fault is None else calls["fault"] > 0
        mistakes = [str(line["mistake"]) for line in lines if "mistake" in line]
        near = {f"{x['call']} {x['method']} {x['path']}" for x in lines if x.get("near_mock")}
        result = RunResult(
            run,
            row,
            outcome,
            answer,
            fired,
            calls,
            mistakes,
            model_calls=len(model),
            cost_usd=cost,
            notices=notices,
            real_errors=real_errors,
            hosted_reads=sum(1 for line in model if "hosted_reads" in line),
            mocked={int(str(line["mock"])) for line in lines if line.get("action") == "mock"},
            tools_seen=reported_tools(notices),
            near_mocks=near,
            tool_calls=sum(1 for line in lines if line.get("kind") in ("tool", "mcp")),
            models={(str(line["call"]), str(line.get("model") or "")) for line in model},
            budget=next(
                (cast(dict[str, object], x["budget"]) for x in lines if "budget" in x), None
            ),
        )
        # Here the raw answer is still at hand. The record only ever gets the redacted one.
        chose = row.fault is not None and (state.refusals or [""])[0] in CHOSEN_CALLS
        refused = [str(line["call"]) for line in lines if line.get("action") == "refused"]
        result.judgement = judge.judge_run(
            outcome,
            answer,
            result.exercised,
            row.rules,
            bool(mistakes),
            refused[0] if chose and outcome == "refused call" else None,
        )
        summary: dict[str, object] = {
            "answer": None if answer is None else redact(answer),
            "outcome": outcome,
            "fault_fired": fired,
            "calls": calls,
            "incomplete": incomplete,
            "model_calls": len(model),
            "cost_usd": round(cost, 8),
            "judged": result.judgement.verdict,
            "broken_rules": [{"kind": b.kind, "text": b.text} for b in result.judgement.broken],
            "left_out": result.judgement.left_out,
        }
        self.store.close_run(run, row.test.name, row.name, summary)
        self.check_labels(result)
        return result

    def answer_of(self, run: str, code: int) -> str | None:
        """The answer file, or for a script that ended well its last printed line."""
        answer_file = self.answers / f"{run}.txt"
        if answer_file.exists():
            return answer_file.read_text(encoding="utf-8", errors="replace")
        return (
            last_line(self.folder / f"{run}.out")
            if self.config.agent.command and not code
            else None
        )

    def line(self, run: str, row: planner.Row, seq: int, pid: object) -> dict[str, object]:
        """A record line the runner writes itself, about a process that sent none."""
        proc = {"host": socket.gethostname(), "pid": pid, "started": now_iso()}
        return {
            "v": RECORD_VERSION,
            "run": run,
            "test": row.test.name,
            "row": row.name,
            "seq": seq,
            "proc": proc,
            "library": "derail",
        }

    def left_line(self, run: str, row: planner.Row, pid: int, command: str) -> dict[str, object]:
        text = redact(command)[:COMMAND_CAP]
        message = f"process {pid} left the run's process group and was killed: {text}"
        return {**self.line(run, row, 1, pid), "notice": "left_group", "message": message}

    def other_python_lines(self, run: str, row: planner.Row) -> list[dict[str, object]]:
        """A child on another Python tried to import a listed tool, and did not get it.

        That Python may not have the SDK, so it left its report beside the answer.
        """
        path = self.answers / f"{run}.txt.refused"
        if not path.exists():
            return []
        lines: list[dict[str, object]] = []
        for number, text in enumerate(path.read_text(errors="replace").splitlines(), 1):
            report, process = read_report(text)
            lines.append(
                {
                    **self.line(run, row, number, report["pid"]),
                    "notice": "other_python",
                    "message": redact(str(report["message"])),
                    "mistake": redact(str(report["message"])),
                    "process": {
                        "python": redact(str(process["python"])),
                        "command": redact(str(process["command"]))[:COMMAND_CAP],
                        "parent": process["parent"],
                    },
                }
            )
        return lines

    def check_labels(self, result: RunResult) -> None:
        """With no tools list, compare the test's labels with the tools the agent has."""
        with self.lock:
            if result.tools_seen is None or self.tools_seen is not None:
                return
            self.tools_seen = result.tools_seen
            self.label_problems = unknown_labels(self.config, result.tools_seen)
            if not self.label_problems:
                return
            self.stopped_by = self.stopped_by or "unknown_label"
        self.cancel()

    def run_all(self) -> list[RunResult]:
        """Every baseline first; then the fault rows of each test whose baseline worked."""
        rows = planner.rows(self.config)
        runs = self.config.agent.runs
        self.server.start()
        previous = {sig: signal.signal(sig, self._interrupted) for sig in STOP_SIGNALS}
        try:
            with ThreadPoolExecutor(max_workers=self.config.agent.parallel) as pool:
                base = [row for row in rows if row.fault is None for _ in range(runs)]
                results: list[RunResult | None] = []
                if not self.config.agent.tools and base:
                    # One run alone first, so a misspelled label costs one run, not all.
                    results.append(self.one(base.pop(0)))
                results += list(pool.map(self.one, base))
                for test in self.config.tests:
                    mine = [r for r in results if r is not None and r.row.test == test]
                    if test.faults and mine and all(r.outcome in FAILED for r in mine):
                        self.skipped.append(test.name)
                rest = [
                    row
                    for row in rows
                    if row.fault is not None and row.test.name not in self.skipped
                    for _ in range(runs)
                ]
                results += list(pool.map(self.one, rest))
        finally:
            self.cancel()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.server.stop()
            shutil.rmtree(self.answers, ignore_errors=True)
        return [r for r in results if r is not None]

    def cancel(self) -> None:
        """Start no more runs and stop every run still going, with its children."""
        with self.lock:
            self.cancelled = True
            running = list(self.procs.values())
        for proc in running:
            stop_group(proc)

    def _interrupted(self, signum: int, frame: object) -> None:
        """The runner itself was told to stop, such as a cancelled CI job."""
        del frame
        self.cancel()
        raise Interrupted(signal.Signals(signum).name)


def real_error(line: dict[str, object]) -> str | None:
    """How a model call the gate let through failed by itself, if it did."""
    if line.get("action") != "real":
        return None
    error = str(line.get("error") or "")
    if error and not error.startswith(NOT_A_FAILURE):
        return f"{line.get('call')} failed with {error.split(':')[0]}"
    status = int(str(line.get("status") or 0))
    return f"{line.get('call')} returned {status}" if status >= 400 else None


class Interrupted(Exception):
    """derail test got SIGTERM or SIGINT."""


def reported_tools(notices: list[dict[str, object]]) -> list[str] | None:
    for line in notices:
        tools = line.get("tools")
        if isinstance(tools, list):
            return [str(t) for t in cast(list[object], tools)]
    return None


def tool_labels(test: Test) -> list[str]:
    return [e.call for e in (*test.mocks, *test.faults) if labels.kind_of(e.call) == "tool"]


def unknown_labels(config: Config, seen: list[str]) -> list[str]:
    """Labels that name no tool the agent has. A misspelled mock or fault lands here."""
    tools = ", ".join(seen) or "none"
    return [
        f"[[test]] {test.name!r}: {label} names no tool the agent has; the tools are {tools}"
        for test in config.tests
        for label in dict.fromkeys(tool_labels(test))
        if label.partition(":")[2] not in seen
    ]


def unused_mocks(config: Config, results: list[RunResult]) -> tuple[list[str], list[str]]:
    """Mocks no run in any row used, as (notes, mistakes).

    A tool's label passed its check, so an unused tool mock is only a note. A web or MCP
    label has no such check, so an unused one may be misspelled: that is a mistake.
    """
    notes: list[str] = []
    mistakes: list[str] = []
    for test in config.tests:
        mine = [r for r in results if r.row.test == test]
        used = {index for r in mine for index in r.mocked}
        if not mine or all(r.outcome in FAILED for r in mine):
            continue
        for index, mock in enumerate(test.mocks):
            if index in used:
                continue
            name = mock.call.partition(":")[2]
            if labels.kind_of(mock.call) == "http":
                mistakes.append(
                    f"[[test]] {test.name!r}: no request matched the mock on {mock.call}, or "
                    "an earlier mock answered it first. Check its host, method and path, or "
                    "remove it."
                )
            elif labels.kind_of(mock.call) == "mcp":
                mistakes.append(
                    f"[[test]] {test.name!r}: no call matched the mock on {mock.call}, or an "
                    "earlier mock answered it first. Check its server and tool, or remove it."
                )
            elif any(other.call == mock.call for other in test.mocks[:index]):
                notes.append(
                    f"in test {test.name!r} an earlier mock on {name} answers every call, so "
                    "the later one was not used. Remove it."
                )
            else:
                notes.append(
                    f"no run in test {test.name!r} called {name}, so its mock was not used. "
                    "The label is correct. Remove the mock, or ask a question that needs the "
                    "tool."
                )
    return notes, mistakes


def read_report(text: str) -> tuple[dict[str, object], dict[str, object]]:
    """A line a child left, and its process. A line it could not finish is still a refusal."""
    try:
        report = cast(dict[str, object], json.loads(text))
        return report, cast(dict[str, object], report["process"])
    except (ValueError, KeyError, TypeError):
        message = "a program on another Python tried to import a listed tool"
        process: dict[str, object] = {"python": "", "command": text, "parent": 0}
        return {"pid": 0, "message": message}, process


def kill_those_who_left(run: str, group: int) -> list[tuple[int, str]]:
    """SIGKILL any process that left the group but still holds this run, as (pid, command).

    A process's own /proc environ is the only proof, and it must hold the exact variable.
    A system with no /proc has no sweep.
    """
    if not Path("/proc/self/environ").exists():
        return []
    wanted = f"DERAIL_RUN={run}".encode()
    killed: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        try:
            if wanted not in (entry / "environ").read_bytes().split(b"\0"):
                continue
            if os.getpgid(pid) == group:
                continue  # one SIGKILL has not finished yet
            words = (entry / "cmdline").read_bytes().split(b"\0")
            command = b" ".join(words).decode(errors="replace")
            os.kill(pid, signal.SIGKILL)
        except OSError:  # another user's, or gone already
            continue
        killed.append((pid, command.strip()))
    return killed


def owner_only(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def last_line(path: Path) -> str | None:
    """The last line of a script's output that is not blank."""
    with path.open("rb") as out:
        out.seek(max(0, path.stat().st_size - TAIL_BYTES))
        tail = out.read().decode("utf-8", "replace")
    return next((line for line in reversed(tail.split("\n")) if line.strip()), None)


def stop_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM to the run's process group, then SIGKILL to what is left after five seconds.

    It waits on the group, not on the program: a child can outlive the program, and can
    ignore SIGTERM after the program has gone.
    """
    if signal_group(proc, signal.SIGTERM):
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            proc.poll()  # a program that has ended stays in the group until it is reaped
            if not signal_group(proc, 0):
                break
            time.sleep(GROUP_POLL_S)
        else:
            signal_group(proc, signal.SIGKILL)
    if proc.poll() is None:  # it left the group by itself
        proc.kill()
    proc.wait()


def signal_group(proc: subprocess.Popen[bytes], sig: int) -> bool:
    """False when no process is left in the group."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS says this for a group of zombies
        return False
    return True


@dataclass(frozen=True)
class RowReport:
    """One row's runs and its result, which the table, the notes and the exit code all read."""

    row: planner.Row
    runs: list[RunResult]
    result: judge.RowResult

    @property
    def where(self) -> str:
        return f"{self.row.test.name} {self.row.name}"

    @property
    def refused(self) -> bool:
        return any(r.outcome == "refused call" for r in self.runs)


def row_reports(config: Config, results: list[RunResult]) -> list[RowReport]:
    reports: list[RowReport] = []
    for row in planner.rows(config):
        mine = [r for r in results if r.row == row]
        refused = any(r.outcome == "refused call" for r in mine)
        result = judge.judge_row(
            [r.judgement for r in mine], config.agent.runs, row.rules.pass_rate, refused
        )
        reports.append(RowReport(row, mine, result))
    return reports


def fault_text(row: planner.Row) -> str:
    if row.fault is None:
        return "-"
    when = "" if row.fault.when == "always" else f" {row.fault.when}"
    return f"{row.fault.target} {row.fault.do}{when}"


def row_cells(report: RowReport) -> list[str]:
    row, mine, result = report.row, report.runs, report.result
    fault = fault_text(row)
    fired = "-" if row.fault is None else str(sum(bool(r.fault_fired) for r in mine))
    rate, span = "-", "-"
    if result.judged:
        low, high = judge.wilson(result.passed, result.judged)
        rate, span = f"{result.passed / result.judged:.0%}", f"{low * 100:.0f}-{high:.0%}"
    tools = sum(r.tool_calls for r in mine) / max(len(mine), 1)
    return [
        row.test.name,
        row.name,
        fault,
        str(len(mine)),
        fired,
        str(sum(1 for r in mine if r.outcome == "answered")),
        f"{result.passed}/{result.judged}",
        rate,
        span,
        f"{tools:.1f}",
        f"{sum(r.cost_usd for r in mine):.5f}",
        result.verdict,
    ]


def fitted(table: list[list[str]]) -> list[int]:
    """Column widths within TABLE_WIDTH. Only the test and fault names can be cut, as every
    line under the table gives them in full; the shorter one keeps its width if it can."""
    widths = [max(len(line[i]) for line in table) for i in range(len(HEAD))]
    test, fault = widths[TEST_COLUMN], widths[FAULT_COLUMN]
    room = TABLE_WIDTH - sum(widths) + test + fault - 2 * (len(HEAD) - 1)
    if test + fault > room:
        widths[TEST_COLUMN] = min(test, max(room // 2, room - fault))
        widths[FAULT_COLUMN] = room - widths[TEST_COLUMN]
    return widths


def cut(text: str, width: int) -> str:
    """Cut from the middle, so a fault's schedule and the end of a long name still show."""
    if len(text) <= width:
        return text
    kept = width - 3
    return f"{text[: kept // 2]}...{text[-(kept - kept // 2) :]}"


def no_result_reason(report: RowReport, runs: int) -> str:
    """Why a row has no result. Only runs left out as unfired call for firing the fault."""
    unfired = any(r.judgement.left_out == judge.NOT_FIRED for r in report.runs)
    if unfired and not any(r.fault_fired for r in report.runs):
        return "the fault never fired"
    judged = report.result.judged
    counted = f"{judged} of {runs} runs were judged, and a result needs {report.result.floor}"
    return f"{counted}. Make the fault fire in every run" if unfired else counted


def unused_model(fault: Fault, runs: list[RunResult]) -> str | None:
    """Why a fault on a model name never fired, as far as the records can say.

    None when the records cannot say: a call under the fault's label has no known name, or a
    refused run was stopped, perhaps just before this model, and may have lost its last line.
    """
    used = {name for run in runs for label, name in run.models if labels.matches(fault.call, label)}
    wanted = repr(fault.model)  # from the file, which allows only printable text: never cut
    if fault.model in used:
        return (
            f"calls used the model {wanted}, yet the fault did not fire: a refused call fires "
            "no fault, and nor does a call in a child process unless [agent] children is true"
        )
    if "" in used or any(run.outcome == "refused call" for run in runs):
        return None
    names = [labels.shown_name(name) for name in sorted(used)]
    more = f" and {len(names) - SHOWN_NAMES} more" if len(names) > SHOWN_NAMES else ""
    told = f"these calls used {', '.join(names[:SHOWN_NAMES])}{more}"
    return f"no call used the model {wanted}; {told if names else 'no such call was made'}"


def where_to_read(broken: judge.Broken, runs: list[RunResult], folder: Path) -> str:
    """Up to three runs that broke a rule: the log if the run ended badly, or else its name,
    which is also its record's name in the folder the first line gives."""
    logs = broken.kind in judge.ENDED_BADLY
    names = [str(folder / f"{r.run}.log") if logs else r.run for r in runs[:SHOWN_RUNS]]
    more = f" and {len(runs) - SHOWN_RUNS} more" if len(runs) > SHOWN_RUNS else ""
    return f"{', '.join(names)}{more}"


def rule_lines(report: RowReport, folder: Path) -> list[str]:
    """Which rules broke, in which runs. Empty when no run broke one."""
    tally: dict[judge.Broken, list[RunResult]] = {}
    for r in report.runs:
        for broken in r.judgement.broken:
            tally.setdefault(broken, []).append(r)
    if not tally:
        return []
    row, rate = report.result, report.row.rules.pass_rate
    word = "fail" if row.verdict == judge.FAIL else "note"
    head = f"{word}: {report.where}: {row.passed}/{row.judged} passed, pass_rate is {rate:g}"
    calls = [b.text for r in report.runs for b in r.judgement.broken if b.kind == judge.REFUSED]
    if calls:
        call = calls[0].removeprefix(f"{judge.REFUSED} ")
        advice = "See the line below" if call == PROCESS_LABEL else "Mocking it would hide this"
        head = (
            f"fail: {report.where}: under this fault the agent called {call}, which the test "
            f"does not allow. {advice}"
        )
    most = sorted(tally.items(), key=lambda item: -len(item[1]))
    return [
        head,
        *(
            f"  {plural(len(runs), 'run')}: {rule.text}: {where_to_read(rule, runs, folder)}"
            for rule, runs in most
        ),
    ]


def counts(ended: Counter[str]) -> str:
    return ", ".join(f"{n} {outcome}" for outcome, n in sorted(ended.items()))


def report(
    config: Config,
    results: list[RunResult],
    folder: Path,
    out: TextIO,
    runner: "Runner | None" = None,
) -> int:
    reports = row_reports(config, results)
    print(
        f"derail test: {plural(len(config.tests), 'test')}, {plural(len(reports), 'row')}, "
        f"{plural(len(results), 'run')}; "
        f"records in {folder}",
        file=out,
    )
    if "tool:*" in config.agent.real:
        print('every tool that is not mocked runs for real (real = ["tool:*"])', file=out)
    table = [HEAD, *map(row_cells, reports)]
    mistakes: list[str] = []
    notes: list[str] = []
    failures: list[str] = []
    code = OK
    for entry in reports:
        row, mine, where = entry.row, entry.runs, entry.where
        verdict = entry.result
        failures += rule_lines(entry, folder)
        if verdict.verdict == judge.NO_RESULT and not entry.refused:
            mistakes.append(f"{where}: no result: {no_result_reason(entry, config.agent.runs)}")
        elif verdict.judged < config.agent.runs:
            notes.append(f"{where}: {verdict.judged} of {config.agent.runs} runs judged")
        ran = [r for r in mine if r.outcome != "stopped"]
        if row.fault is not None and ran and not any(r.fault_fired for r in ran):
            mistakes.append(f"{where}: the fault never fired in any run, so the row tested nothing")
            mixed = [m.call for m in row.test.mocks if labels.kind_of(m.call) == "tool"]
            if labels.kind_of(row.fault.call) in ("http", "mcp") and mixed:
                mistakes.append(
                    f"{where}: the mock on {', '.join(mixed)} replaces the whole tool, so if "
                    f"that tool makes this call, {row.fault.call} can never fire; "
                    "mock or fault one level, not both"
                )
            if row.fault.model is not None and (why := unused_model(row.fault, ran)):
                mistakes.append(f"{where}: {why}")
        notes += [
            f"{where}: {r.run}: {line['message']}"
            for r in mine
            for line in r.notices
            if line.get("notice") == "left_group"
        ]
        ended = Counter(r.outcome for r in mine if not r.exercised)
        if ended:
            notes.append(f"{where}: not exercised, the fault did not fire: {counts(ended)}")
        ended = Counter(r.outcome for r in mine if r.exercised and r.outcome != "answered")
        if ended:
            notes.append(f"{where}: {counts(ended)}")
        for r in mine:
            if r.outcome == "refused call":
                mistakes.append(
                    f"{where}: {r.run} made a call the test does not allow; see its record"
                )
            # "Mock it" would silence a refused call. A blind child's line holds its fix.
            shown = not any(
                b.kind == judge.REFUSED and not b.text.endswith(PROCESS_LABEL)
                for b in r.judgement.broken
            )
            mistakes += [f"{where}: {r.run}: {m}" for m in r.mistakes if shown]
            if r.outcome == "infrastructure error":
                code = INFRA
    stopped_by = runner.stopped_by if runner is not None else None
    unused, wrong = unused_mocks(config, results) if stopped_by is None else ([], [])
    mistakes += wrong
    if runner is not None and not config.agent.tools:
        mistakes += runner.label_problems
        if stopped_by is None and runner.tools_seen is not None:
            notes += unused
        elif stopped_by is None and results and any(map(tool_labels, config.tests)):
            mistakes.append(
                "no run reported the agent's tools, so the tool labels in this file were not "
                "checked; see the run logs"
            )
    skipped = runner.skipped if runner is not None else []
    for name in skipped:
        mistakes.append(
            f"{name}: baseline failed: every baseline run crashed, timed out, was refused or "
            "reached the budget, so its fault rows did not run"
        )
    if stopped_by in ("budget", "budget_held"):
        numbers = next((r.budget for r in results if r.budget is not None), None)
        line = (
            budget_line(stopped_by, numbers, config.agent.reserve_output_tokens)
            if numbers is not None
            else f"the budget of ${config.agent.max_cost_usd:g} stopped the test"
        )
        mistakes.append(f"{line}; rows without runs have no result")
    budget_on = config.agent.max_cost_usd is not None
    for test in config.tests:
        baseline = [r for r in results if r.row.test == test and r.row.fault is None]
        ran = [r for r in baseline if r.outcome != "stopped"]
        # A refused or broken run may never have reached a model call: that proves nothing.
        ended = all(r.outcome in ("answered", "crashed", "timed out") for r in ran)
        if budget_on and ran and ended and not any(r.model_calls for r in ran):
            if any(r.outcome == "answered" for r in ran):
                mistakes.append(
                    f"{test.name}: no baseline run made a model call, so the budget is not "
                    "working; check for a cache in front of the model or a client Derail "
                    "does not gate"
                )
            else:  # the agent never got that far, which says nothing about the budget
                mistakes.append(
                    f"{test.name}: every baseline run crashed or timed out before its first "
                    "model call, so the test shows nothing; see the run logs"
                )
    silent = [r for r in results if r.model_calls == 0 and r.outcome == "answered"]
    for r in results:
        for line in r.notices:
            if line.get("notice") == "structure":
                code = INFRA
                mistakes.append(f"{line.get('library')}: {line.get('message')}")
        for error in r.real_errors:
            code = INFRA
            mistakes.append(f"{r.row.test.name} {r.row.name}: {r.run}: {error}, not injected")
    hosted = sum(r.hosted_reads for r in results)
    near_mocks = sorted({call for r in results for call in r.near_mocks})
    widths = fitted(table)
    for line in table:
        print(
            "  ".join(cut(cell, w).ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip(),
            file=out,
        )
    for entry, line in zip(reports, table[1:], strict=True):
        if len(line[TEST_COLUMN]) > widths[TEST_COLUMN] or (
            len(line[FAULT_COLUMN]) > widths[FAULT_COLUMN]
        ):
            print(f"full name: {entry.where}: {line[FAULT_COLUMN]}", file=out)
    print("range: a 95% Wilson interval, from these runs alone", file=out)
    for line in failures:
        print(line, file=out)
    spent = sum(r.cost_usd for r in results)
    limit = "no budget" if not budget_on else f"a budget of ${config.agent.max_cost_usd:g}"
    print(f"model calls cost ${spent:.4f} of {limit}", file=out)
    if budget_on and spent > (config.agent.max_cost_usd or 0):
        print(
            f"warning: the test spent more than its budget of ${config.agent.max_cost_usd:g}, "
            "because the last calls cost more than was reserved for them",
            file=out,
        )
    for call in near_mocks:
        print(
            f"warning: {call} ran for real, though the test has mocks for that host; "
            "none matched its method and path",
            file=out,
        )
    if hosted:
        print(
            f"warning: {plural(hosted, 'model request')} offered provider tools that only read, "
            "such as web search; providers bill those per use, outside the token budget",
            file=out,
        )
    for message in notes:
        print(f"note: {message}", file=out)
    if silent:
        print(f"note: {plural(len(silent), 'run')} answered with no model call", file=out)
    warnings = {
        str(line.get("message"))
        for r in results
        for line in r.notices
        if line.get("notice") == "newer_version"
    }
    for warning in sorted(warnings):
        print(f"warning: {warning}", file=out)
    for message in dict.fromkeys(mistakes):
        print(f"test mistake: {message}", file=out)
    if code == INFRA:
        print(
            "infrastructure error: see the runs marked so above, and their .log files",
            file=out,
        )
        return INFRA
    if mistakes:
        return MISTAKE
    return ROW_FAILED if any(r.result.verdict == judge.FAIL for r in reports) else OK


def budget_line(reason: str, numbers: dict[str, object], reserve_tokens: int) -> str:
    """Why the budget stopped the test. "Reached" only when money spent reached the limit:
    a call can be refused while most of the budget is only held by calls in flight."""
    usd, spent, held, limit, waited = (
        float(str(numbers.get(key, 0))) for key in ("usd", "spent", "held", "limit", "waited_s")
    )
    if spent >= limit:
        return f"the budget of ${limit:g} was reached: ${spent:.4f} was spent, so the test stopped"
    wait = f", after a wait of {waited:.0f}s for calls in flight" if reason == "budget_held" else ""
    return (
        f"a model call could not be reserved: it could cost up to ${usd:.4f}, ${spent:.4f} was "
        f"spent and ${held:.4f} was held by calls in flight, of a budget of ${limit:g}{wait}, so "
        f"the test stopped; set max_tokens on the call or lower [agent] reserve_output_tokens "
        f"({reserve_tokens}) to hold less, or raise [agent] max_cost_usd"
    )


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
