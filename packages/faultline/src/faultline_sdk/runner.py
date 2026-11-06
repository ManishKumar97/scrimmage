"""The runner for a function agent: one new program per run, the run table, exit codes.

Exit codes: 0 when every row produced a result; 2 for a mistake in the test, a refused
call and a test the budget stopped included; 3 for an infrastructure error.
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
from faultline_sdk.budget import Ledger
from faultline_sdk.config import Config
from faultline_sdk.control import ControlServer, proc_key
from faultline_sdk.records import RecordStore
from faultline_sdk.redact import redact

OK, MISTAKE, INFRA = 0, 2, 3
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
KILL_GRACE_S = 5.0
OUTCOMES = (
    "answered",
    "crashed",
    "timed out",
    "refused call",
    "budget reached",
    "infrastructure error",
    "stopped",
)
# A refusal for one of these reasons stops the whole test, not only its row.
STOP_TEST = ("budget", "no_price", "unknown_tool_type", "unseen_tool", "structure")
# The agent's own cancellation is not a failure of the provider.
NOT_A_FAILURE = ("CancelledError", "KeyboardInterrupt", "SystemExit", "GeneratorExit")
# A baseline whose every run ended so cannot be compared with, so its faults are skipped.
FAILED = ("crashed", "timed out", "refused call", "budget reached")


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

    @property
    def exercised(self) -> bool:
        return self.fault_fired is not False or self.outcome == "stopped"


class Runner:
    def __init__(self, config: Config, control_url: Callable[[str], str] = lambda url: url) -> None:
        self.config = config
        self.control_url = control_url  # lets a test point the agent elsewhere
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        runs = config.root / "faultline-runs"
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
        self.answers = Path(tempfile.mkdtemp(prefix="faultline-answers-"))
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.stopped_rows: set[tuple[str, str]] = set()
        self.row_of: dict[str, planner.Row] = {}
        self.cancelled = False
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
        if proc is not None:
            stop_group(proc)
        if stop_test:
            self.cancel()

    def env(self, run: str) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("FAULTLINE_ROOT_PID", None)  # faultline-run sets it for this run's program
        env.update(
            FAULTLINE_TOKEN=self.token,
            FAULTLINE_CONTROL_URL=self.control_url(self.server.url),
            FAULTLINE_RUN=run,
            FAULTLINE_ANSWER_FILE=str(self.answers / f"{run}.txt"),
            FAULTLINE_ALLOW_IMPORTS=",".join(self.config.agent.allowed_imports()),
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
            *([self.config.agent.answer] if self.config.agent.answer else []),
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
        # Refused, or the test was stopped, before this program was registered.
        if self.server.runs[run].refused or self.cancelled:
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
        model = [line for line in lines if line.get("kind") == "model"]
        notices = [line for line in lines if "notice" in line]
        cost = sum(float(str(line.get("cost_usd") or 0)) for line in model)
        # The provider failing by itself says nothing about the agent.
        real_errors = [e for e in map(real_error, model) if e is not None]
        broken = any(n.get("notice") == "structure" for n in notices)
        if stopped:
            outcome = "stopped"
        elif not state.fetched or incomplete or real_errors or broken:
            outcome = "infrastructure error"
        elif state.refusals[:1] == ["budget"]:
            outcome = "budget reached"
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
            "model_calls": len(model),
            "cost_usd": round(cost, 8),
        }
        self.store.close_run(run, row.test.name, row.name, summary)
        return RunResult(
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
        )

    def run_all(self) -> list[RunResult]:
        """Every baseline first; then the fault rows of each test whose baseline worked."""
        rows = planner.rows(self.config)
        runs = self.config.agent.runs
        self.server.start()
        previous = {sig: signal.signal(sig, self._interrupted) for sig in STOP_SIGNALS}
        try:
            with ThreadPoolExecutor(max_workers=self.config.agent.parallel) as pool:
                base = [row for row in rows if row.fault is None for _ in range(runs)]
                results = list(pool.map(self.one, base))
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


def report(
    config: Config,
    results: list[RunResult],
    folder: Path,
    out: TextIO,
    runner: "Runner | None" = None,
) -> int:
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
        exercised = "-" if row.fault is None else str(sum(bool(r.fault_fired) for r in mine))
        table.append(
            [row.test.name, row.name, fault, str(len(mine)), exercised]
            + [str(sum(1 for r in counted if r.outcome == o)) for o in OUTCOMES]
        )
        where = f"{row.test.name} {row.name}"
        ran = [r for r in mine if r.outcome != "stopped"]
        if row.fault is not None and ran and not any(r.fault_fired for r in ran):
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
    stopped_by = runner.stopped_by if runner is not None else None
    skipped = runner.skipped if runner is not None else []
    for name in skipped:
        mistakes.append(
            f"{name}: baseline failed: every baseline run crashed, timed out, was refused or "
            "reached the budget, so its fault rows did not run"
        )
    if stopped_by == "budget":
        mistakes.append(
            f"the budget of ${config.agent.max_cost_usd:g} was reached, so the test stopped; "
            "rows without runs have no result"
        )
    budget_on = config.agent.max_cost_usd is not None
    for test in config.tests:
        baseline = [r for r in results if r.row.test == test and r.row.fault is None]
        ran = [r for r in baseline if r.outcome != "stopped"]
        # A refused or broken run may never have reached a model call: that proves nothing.
        ended = all(r.outcome in ("answered", "crashed", "timed out") for r in ran)
        if budget_on and ran and ended and not any(r.model_calls for r in ran):
            mistakes.append(
                f"{test.name}: no baseline run made a model call, so the budget is not working; "
                "check for a cache in front of the model or a client Faultline does not gate"
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
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    for line in table:
        print(
            "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip(),
            file=out,
        )
    spent = sum(r.cost_usd for r in results)
    limit = "no budget" if not budget_on else f"a budget of ${config.agent.max_cost_usd:g}"
    print(f"model calls cost ${spent:.4f} of {limit}", file=out)
    if budget_on and spent > (config.agent.max_cost_usd or 0):
        print(
            f"warning: the test spent more than its budget of ${config.agent.max_cost_usd:g}, "
            "because the last calls cost more than was reserved for them",
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
    return MISTAKE if mistakes else OK


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
