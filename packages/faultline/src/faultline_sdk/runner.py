"""The runner for a function agent: one new program per run, the run table, exit codes.

Exit codes: 0 when every row produced a result; 2 for a mistake in the test, a refused
call included; 3 for an infrastructure error.
"""

import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from faultline_sdk import plan as planner
from faultline_sdk.config import Config
from faultline_sdk.control import ControlServer, proc_key
from faultline_sdk.mocks import allowed
from faultline_sdk.records import RecordStore
from faultline_sdk.redact import redact

OK, MISTAKE, INFRA = 0, 2, 3
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
KILL_GRACE_S = 5.0
OUTCOMES = ("answered", "crashed", "timed out", "refused call", "infrastructure error")


@dataclass
class RunResult:
    run: str
    row: planner.Row
    outcome: str
    answer: str | None
    fault_fired: bool | None
    calls: dict[str, int]
    mistakes: list[str] = field(default_factory=list[str])

    @property
    def exercised(self) -> bool:
        return self.fault_fired is not False


def check_labels(config: Config) -> list[str]:
    """Problems found before the first run, so a typo costs no model calls."""
    names = config.tool_names()
    problems: list[str] = []
    for test in config.tests:
        for entry in [*test.mocks, *test.faults]:
            name = entry.call.partition(":")[2]
            if name not in names:
                problems.append(
                    f"[[test]] {test.name!r}: {entry.call} names no tool in [agent] tools; "
                    f"the tools are {', '.join(names) or 'none'}"
                )
        for mock in test.mocks:
            if mock.type and not allowed(mock.type, config.allowed_imports()):
                problems.append(
                    f"[[test]] {test.name!r}: type {mock.type} is outside the agent's packages "
                    f"({', '.join(config.allowed_imports())}), so it is refused"
                )
    for entry in config.agent.real:
        name = entry.partition(":")[2]
        if name != "*" and name not in names:
            problems.append(f"[agent] real: {entry} names no tool in [agent] tools")
    return problems


class Runner:
    def __init__(self, config: Config, control_url: Callable[[str], str] = lambda url: url) -> None:
        self.config = config
        self.control_url = control_url  # lets a test point the agent elsewhere
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        runs = config.root / "faultline-runs"
        self.folder = runs / f"{stamp}-{secrets.token_hex(3)}"
        self.store = RecordStore(self.folder)
        # Records can hold customer data, so they stay out of the team's git repo.
        (runs / ".gitignore").write_text("*\n")
        self.token = secrets.token_hex(32)
        self.server = ControlServer(self.token, self.store, on_refused=self._refused)
        self.answers = Path(tempfile.mkdtemp(prefix="faultline-answers-"))
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.stopped_rows: set[tuple[str, str]] = set()
        self.row_of: dict[str, planner.Row] = {}
        self.cancelled = False

    def _refused(self, run: str) -> None:
        """The first refusal ends its run and its row."""
        with self.lock:
            row = self.row_of[run]
            self.stopped_rows.add((row.test.name, row.name))
            proc = self.procs.get(run)
        if proc is not None:
            stop_group(proc)

    def env(self, run: str) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("FAULTLINE_ROOT_PID", None)  # faultline-run sets it for this run's program
        env.update(
            FAULTLINE_TOKEN=self.token,
            FAULTLINE_CONTROL_URL=self.control_url(self.server.url),
            FAULTLINE_RUN=run,
            FAULTLINE_ANSWER_FILE=str(self.answers / f"{run}.txt"),
            FAULTLINE_ALLOW_IMPORTS=",".join(self.config.allowed_imports()),
        )
        return env

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
        command = [
            sys.executable, "-m", "faultline_sdk.run_command",
            sys.executable, "-m", "faultline_sdk.start",
            self.config.agent.function, row.test.question,
        ]  # fmt: skip
        log = os.open(self.folder / f"{run}.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            proc = subprocess.Popen(
                command,
                cwd=self.config.root,
                env=self.env(run),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # its own process group, so every child stops too
            )
        finally:
            os.close(log)
        with self.lock:
            self.procs[run] = proc
        if self.server.runs[run].refused:  # refused before the process was registered
            stop_group(proc)
        timed_out = False
        try:
            proc.wait(timeout=self.config.agent.run_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_group(proc)
        with self.lock:
            del self.procs[run]
        state = self.server.close_run(run)
        lines = self.store.lines(run)
        answer_file = self.answers / f"{run}.txt"
        answer = answer_file.read_text() if answer_file.exists() else None
        ended_itself = not timed_out and not state.refused
        # Every process that ended by itself sent a last batch saying how many lines it
        # made. A missing batch, or fewer lines received from a process, is a gap.
        per_proc = Counter(proc_key(line.get("proc")) for line in lines)
        gap = any(per_proc[proc] < made for proc, made in state.made.items())
        unfinished = not state.made or any(proc not in state.made for proc in per_proc)
        incomplete = bool(state.dropped) or (ended_itself and (gap or unfinished))
        if not state.fetched or incomplete:
            outcome = "infrastructure error"
        elif state.refused:
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
        summary: dict[str, object] = {
            "answer": None if answer is None else redact(answer),
            "outcome": outcome,
            "fault_fired": fired,
            "calls": calls,
            "incomplete": incomplete,
        }
        self.store.close_run(run, row.test.name, row.name, summary)
        return RunResult(run, row, outcome, answer, fired, calls, mistakes)

    def run_all(self) -> list[RunResult]:
        jobs = [row for row in planner.rows(self.config) for _ in range(self.config.agent.runs)]
        self.server.start()
        previous = {sig: signal.signal(sig, self._interrupted) for sig in STOP_SIGNALS}
        try:
            with ThreadPoolExecutor(max_workers=self.config.agent.parallel) as pool:
                results = list(pool.map(self.one, jobs))
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


class Interrupted(Exception):
    """faultline test got SIGTERM or SIGINT."""


def stop_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM to the run's process group, then SIGKILL after five seconds."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:  # the group is gone; make sure the program is too
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def report(config: Config, results: list[RunResult], folder: Path, out: TextIO) -> int:
    rows = planner.rows(config)
    print(
        f"faultline test: {plural(len(config.tests), 'test')}, {plural(len(rows), 'row')}, "
        f"{plural(len(results), 'run')}; "
        f"records in {folder}",
        file=out,
    )
    if "tool:*" in config.agent.real:
        print('every tool that is not mocked runs for real (real = ["tool:*"])', file=out)
    head = ["test", "row", "fault", "runs", "exercised", *OUTCOMES]
    table: list[list[str]] = [head]
    mistakes: list[str] = []
    notes: list[str] = []
    code = OK
    for row in rows:
        mine = [r for r in results if r.row == row]
        counted = [r for r in mine if r.exercised]
        fault = "-" if row.fault is None else f"{row.fault.call} {row.fault.do}"
        exercised = "-" if row.fault is None else str(len(counted))
        table.append(
            [row.test.name, row.name, fault, str(len(mine)), exercised]
            + [str(sum(1 for r in counted if r.outcome == o)) for o in OUTCOMES]
        )
        where = f"{row.test.name} {row.name}"
        if row.fault is not None and mine and not counted:
            mistakes.append(f"{where}: the fault never fired in any run, so the row tested nothing")
        ended = Counter(r.outcome for r in mine if not r.exercised)
        if ended:
            how = ", ".join(f"{n} {outcome}" for outcome, n in sorted(ended.items()))
            notes.append(f"{where}: not exercised, the fault did not fire: {how}")
        for r in mine:
            if r.outcome == "refused call":
                mistakes.append(
                    f"{where}: {r.run} made a call the test does not allow; see its record"
                )
            mistakes += [f"{where}: {r.run}: {m}" for m in r.mistakes]
            if r.outcome == "infrastructure error":
                code = INFRA
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    for line in table:
        print(
            "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip(),
            file=out,
        )
    for message in notes:
        print(f"note: {message}", file=out)
    for message in dict.fromkeys(mistakes):
        print(f"test mistake: {message}", file=out)
    if code == INFRA:
        print(
            "infrastructure error: see the runs marked so above, and their .log files",
            file=out,
        )
        return INFRA
    return MISTAKE if mistakes else OK


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
