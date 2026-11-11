"""The runner: one new program per run, the run table, exit codes.

Exit codes: 0 when every row passed; 1 when a row failed; 2 for a mistake in the test, a
refused call and a test the budget stopped included; 3 for an infrastructure error. The
higher code wins, as a mistake or an error makes the rates not worth trusting.
"""

import contextlib
import dataclasses
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
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from scrimmage_core import judge, labels
from scrimmage_core import plan as planner
from scrimmage_core.budget import Ledger
from scrimmage_core.config import QUESTION, Agent, Config, Fault, Mock, Test
from scrimmage_core.control import ControlServer, proc_key
from scrimmage_core.outcomes import (  # the runner's names for them
    INFRA,
    INFRA_ERRORS,
    MISTAKE,
    MISTAKES,
    NOTES,
    OK,
    PREVIEW,
    ROW_FAILED,
    WARNINGS,
    plural,
)
from scrimmage_core.records import RECORD_VERSION, RecordStore
from scrimmage_core.redact import redact
from scrimmage_core.render import HEAD, miss_text, row_cells, table_lines
from scrimmage_core.results import HASH_VERSION, row_hash
from scrimmage_core.state import COMMAND_CAP, now_iso

END = b"\x00"  # ends the stop watcher; no signal has the number 0
TAIL_BYTES = 1024 * 1024  # how much of a script's output is read to find its last line
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
KILL_GRACE_S = 5.0
GROUP_POLL_S = 0.01
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
UNKNOWN_LABEL = "unknown_label"  # a label named no tool the agent's first run reported
STOPPED_BY = (*STOP_TEST, UNKNOWN_LABEL)  # every reason that can stop the whole test
# The agent's own cancellation is not a failure of the provider.
NOT_A_FAILURE = (
    "CancelledError",
    "KeyboardInterrupt",
    "SystemExit",
    "GeneratorExit",
    "ScrimmageStopped",
)
# The agent chose this call. Under a fault that is what it did about the failure.
CHOSEN_CALLS = ("unlisted", "web_write", "blind_child")
PROCESS_LABEL = "process:python"
# A baseline whose every run ended so cannot be compared with, so its faults are skipped.
FAILED = ("crashed", "timed out", "refused call", "budget reached", "not reserved")
BUDGET_REASONS = ("budget", "budget_held")  # refusals for the budget
SHOWN_RUNS = 3  # how many runs a line names for one broken rule
SHOWN_NAMES = 5  # how many model names a line lists
# The lists a report's messages go in, and the word each line starts with in the text.
PREFIXES = {
    MISTAKES: "test mistake",
    NOTES: "note",
    WARNINGS: "warning",
    INFRA_ERRORS: "infrastructure error",
}
TOOL_STAR_LINE = 'every tool that is not mocked runs for real (real = ["tool:*"])'
# Said in the text and in --json alike, so it names a field both can show.
INFRA_LINE = (
    "see each run whose outcome is infrastructure error, and its .log file in the records folder"
)
NOT_JUDGED = judge.RunJudgement(left_out="not judged")


def is_exercised(fired: bool | None, outcome: str) -> bool:
    """A run tested its fault: it fired (or the row has none), or the test was stopped."""
    return fired is not False or outcome == "stopped"


def fault_fired(has_fault: bool, fault_lines: int) -> bool | None:
    """None on a baseline, else whether the gate injected the fault at least once."""
    return fault_lines > 0 if has_fault else None


def chose_refused(has_fault: bool, first_refusal: str) -> bool:
    """The agent chose a call the test does not allow, under a fault."""
    return has_fault and first_refusal in CHOSEN_CALLS


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
    # calls that had a mock on their label and matched none: the line's mock_miss and call
    misses: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    tool_calls: int = 0
    models: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])  # label, name
    budget: dict[str, object] | None = None  # the numbers of its first refusal, if for the budget
    budget_reason: str = ""  # the reason of its first refusal for the budget
    judgement: judge.RunJudgement = NOT_JUDGED

    @property
    def miss_count(self) -> int:
        return len(self.misses)

    @property
    def exercised(self) -> bool:
        return is_exercised(self.fault_fired, self.outcome)


class Runner:
    def __init__(
        self,
        config: Config,
        control_url: Callable[[str], str] = lambda url: url,
        stop_flag: list[str] | None = None,
        *,
        record: bool = False,
        probe: bool = False,
    ) -> None:
        self.config = config
        self.record = record  # scrimmage record: real calls' lines carry what a mock needs
        self.probe = probe  # scrimmage init --probe: the first model request ends the run
        self.control_url = control_url  # lets a test point the agent elsewhere
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        runs = config.root / "scrimmage-runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runs, 0o700)  # records hold customer data: only their owner may list them
        self.store = new_store(runs, stamp)
        self.folder = self.store.folder
        # Records can hold customer data, so they stay out of the team's git repo.
        ignore = runs / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        self.token = secrets.token_hex(32)
        self.ledger = Ledger(config.agent.max_cost_usd)
        self.server = ControlServer(
            self.token, self.store, on_refused=self._refused, ledger=self.ledger
        )
        self.answers = Path(tempfile.mkdtemp(prefix="scrimmage-answers-"))
        self.lock = threading.Lock()
        self.ended = False  # run_all has read and cleared stop_flag; the watcher adds nothing
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.stopped_rows: set[tuple[str, str]] = set()
        self.row_of: dict[str, planner.Row] = {}
        self.cancelled = False
        self.tools_seen: list[str] | None = None  # as the agent's process reported them
        self.label_problems: list[str] = []
        self.stopped_by: str | None = None  # the reason a refusal stopped the whole test
        self.skipped: list[str] = []  # tests whose fault rows did not run: baseline failed
        # The name of the signal that stops the test. A watcher thread learns of it through
        # signal.set_wakeup_fd, which Python's C handler writes at once, on whatever thread
        # the signal lands; it then stops the runs. Nothing is raised in a handler, which a
        # __del__ it fired in would swallow, and nothing waits for the main thread, which
        # sleeps in the pool's wait and runs a Python handler only once it wakes.
        self.stop_flag: list[str] = [] if stop_flag is None else stop_flag

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
        for name in ("SCRIMMAGE_ROOT_PID", "SCRIMMAGE_PYTHON", "SCRIMMAGE_CHILDREN"):
            env.pop(name, None)  # the program sets the first two for itself
        env.update(
            SCRIMMAGE_RUNNER_PID=str(os.getpid()),  # run_command asks to be stopped with it
            SCRIMMAGE_TOKEN=self.token,
            SCRIMMAGE_CONTROL_URL=self.control_url(self.server.url),
            SCRIMMAGE_RUN=run,
            SCRIMMAGE_ANSWER_FILE=str(self.answers / f"{run}.txt"),
            SCRIMMAGE_ALLOW_IMPORTS=",".join(agent.allowed_imports()),
            SCRIMMAGE_TOOLS=planner.tools_text(agent.tools, agent.renames),
        )
        if agent.children:
            env["SCRIMMAGE_CHILDREN"] = "1"
        if self.probe:  # nothing reaches a model, so a key is needed only to build the client
            env.setdefault("OPENAI_API_KEY", "scrimmage-probe-sends-no-request")
        return env

    def program(self, question: str) -> list[str]:
        """What one run starts. A script's command was split when the file was read, so the
        question stays one argument, and no shell ever sees it."""
        agent = self.config.agent
        if agent.command:
            return [part.replace(QUESTION, question) for part in agent.command]
        answer = [agent.answer] if agent.answer else []
        return [sys.executable, "-m", "scrimmage_core.start", agent.function, question, *answer]

    def one(self, row: planner.Row) -> RunResult | None:
        with self.lock:
            if self.cancelled or (row.test.name, row.name) in self.stopped_rows:
                return None
            run = f"run-{secrets.token_hex(6)}"
            self.row_of[run] = row
        plan = planner.plan_for(self.config, row, run)
        if self.record:
            plan = dataclasses.replace(plan, record=True)
        if self.probe:
            plan = dataclasses.replace(plan, probe=True)
        self.server.open_run(run, row.name, plan.to_json())
        if self.cancelled:
            self.server.close_run(run)
            return None
        command = [
            sys.executable,
            "-m",
            "scrimmage_core.run_command",
            *self.program(row.test.question),
        ]
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
        timed_out = wait_run(proc, self.config.agent.run_timeout)
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
        first = (state.refusals or [""])[0]
        refusal = budget_refusal(lines)
        numbers = None if refusal is None else cast(dict[str, object], refusal["budget"])
        if stopped:
            outcome = "stopped"
        elif not state.fetched or incomplete or real_errors or broken or no_plan:
            outcome = "infrastructure error"
        elif first in BUDGET_REASONS:
            outcome = "budget reached" if numbers and reached(numbers) else "not reserved"
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
        fired = fault_fired(row.fault is not None, calls["fault"])
        mistakes = [str(line["mistake"]) for line in lines if "mistake" in line]
        misses = [
            {
                "call": str(x["call"]),
                "action": str(x["action"]),
                **cast(dict[str, object], x["mock_miss"]),
            }
            for x in lines
            if "mock_miss" in x
        ]
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
            misses=misses,
            tool_calls=sum(1 for line in lines if line.get("kind") in ("tool", "mcp")),
            models={(str(line["call"]), str(line.get("model") or "")) for line in model},
            budget=numbers,
            budget_reason="" if refusal is None else str(refusal["reason"]),
        )
        # Here the raw answer is still at hand. The record only ever gets the redacted one.
        chose = chose_refused(row.fault is not None, (state.refusals or [""])[0])
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
            "library": "scrimmage",
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
            self.stopped_by = self.stopped_by or UNKNOWN_LABEL
        self.cancel()

    def run_all(self) -> list[RunResult]:
        """Every baseline first; then the fault rows of each test whose baseline worked."""
        rows = planner.rows(self.config)
        runs = self.config.agent.runs
        self.server.start()
        self.ended = False
        release = nothing
        try:
            release = self._catch_signals()
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
            with self.lock:
                self.ended = True  # from here the watcher notes nothing, so nothing is noted twice
                name = self.stop_flag[0] if self.stop_flag else ""
                self.stop_flag.clear()  # the watcher and the handler may both have noted it
            if name:  # every run has ended, so it is raised where nothing drops it
                raise Interrupted(name)
        finally:
            release()
            self.cancel()
            self.server.stop()
            shutil.rmtree(self.answers, ignore_errors=True)
        return [r for r in results if r is not None]

    def _catch_signals(self) -> Callable[[], None]:
        """Stop the runs on SIGINT or SIGTERM. Returns the function that puts everything back.

        Python lets only the main thread set a handler or a wakeup fd. From another thread
        nothing is set, and the runs stop only through stop_flag and cancel().
        """
        if threading.current_thread() is not threading.main_thread():
            return nothing
        previous: dict[signal.Signals, object] = {}
        pipe = (-1, -1)
        old_wakeup = -1
        wakeup_set = False
        watcher: threading.Thread | None = None

        def release() -> None:
            # Handlers and the wakeup fd first: from here a signal goes to the caller's, never
            # to a closed pipe.
            if wakeup_set:
                signal.set_wakeup_fd(old_wakeup)
            for sig, handler in previous.items():
                # None: set outside Python, which cannot be put back, so the default is.
                signal.signal(sig, signal.SIG_DFL if handler is None else handler)  # pyright: ignore[reportArgumentType]
            if watcher is not None:
                os.set_blocking(pipe[1], True)  # a full pipe must not keep the watcher waiting
                os.write(pipe[1], END)  # the watcher returns
                watcher.join()
            for fd in pipe:
                if fd != -1:
                    os.close(fd)

        try:
            for sig in STOP_SIGNALS:
                previous[sig] = signal.signal(sig, self._interrupted)
            pipe = os.pipe()
            os.set_blocking(pipe[1], False)  # set_wakeup_fd needs it, and a full pipe is fine
            old_wakeup = signal.set_wakeup_fd(pipe[1])
            wakeup_set = True
            watcher = threading.Thread(
                target=self._watch, args=(pipe[0], old_wakeup), name="scrimmage-stop", daemon=True
            )
            watcher.start()
            if self.stop_flag:  # noted just before the handlers above were set
                os.write(pipe[1], bytes([STOP_SIGNALS[0]]))
                self.cancel()  # at once: no run may start before the watcher has read the byte
        except BaseException:
            release()
            raise
        return release

    def cancel(self) -> None:
        """Start no more runs and stop every run still going, with its children."""
        with self.lock:
            self.cancelled = True
            running = list(self.procs.values())
        stop_groups(running)

    def _interrupted(self, signum: int, frame: object) -> None:
        """The runner itself was told to stop, such as a cancelled CI job. Only notes it."""
        del frame
        if not self.stop_flag:
            self.stop_flag.append(signal.Signals(signum).name)

    def _watch(self, wake_read: int, forward: int) -> None:
        """Stop every run once a stop signal comes; at the end of run_all, return.

        The wakeup fd also carries every other signal a caller catches. Each is passed on to the
        fd the caller had, as Python's own handler would have written it, until the end.
        """
        stopped = False
        while (byte := os.read(wake_read, 1)) != END:
            if not byte:
                return
            if byte[0] in STOP_SIGNALS:
                with self.lock:
                    if not self.ended and not self.stop_flag:
                        self.stop_flag.append(signal.Signals(byte[0]).name)
                if not stopped:
                    stopped = True
                    self.cancel()
            elif forward != -1:
                with contextlib.suppress(OSError):  # a full or closed pipe drops the byte
                    os.write(forward, byte)


FOLDER_TRIES = 5


def folder_suffix() -> str:
    return secrets.token_hex(3)


def new_store(runs: Path, stamp: str) -> RecordStore:
    """A new records folder. One that exists already, from a test started in the same second
    with the same suffix, is never shared: a new suffix is drawn, a few times."""
    for _ in range(FOLDER_TRIES):
        try:
            return RecordStore(runs / f"{stamp}-{folder_suffix()}")
        except FileExistsError:
            continue
    raise RuntimeError(f"could not make a new records folder in {runs}: {FOLDER_TRIES} tries")


def nothing() -> None:
    """The release of a signal setup that set nothing."""


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
    """scrimmage test got SIGTERM or SIGINT."""


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


def unused_mocks(
    config: Config, results: list[RunResult]
) -> tuple[list[str], list[str], list[str]]:
    """Mocks no run in any row used, as (notes, mistakes, recorded).

    A tool's label passed its check, so an unused tool mock is only a note. A web or MCP
    label has no such check, so an unused one may be misspelled: that is a mistake. A mock
    scrimmage record wrote cannot be misspelled, and with its exact arguments one a later run
    does not repeat goes unused by design, so it is a note, whatever its label or tools list.
    """
    notes: list[str] = []
    mistakes: list[str] = []
    recorded: list[str] = []
    for test in config.tests:
        mine = [r for r in results if r.row.test == test]
        used = {index for r in mine for index in r.mocked}
        if not mine or all(r.outcome in FAILED for r in mine):
            continue
        nearest = {
            int(str(cast(dict[str, object], m["nearest"])["mock"]))
            for r in mine
            for m in r.misses
            if m.get("nearest") is not None
        }
        for index, mock in enumerate(test.mocks):
            if index in used:
                continue
            name = mock.call.partition(":")[2]
            if mock.recorded:
                said = (
                    "a call with other arguments missed it; see the warning"
                    if index in nearest
                    else "this run did not make that call"
                )
                recorded.append(
                    f"in test {test.name!r} the recorded mock on {recorded_call(mock)} was not "
                    f"used; {said}"
                )
            elif (
                mock.args is not None
                and labels.kind_of(mock.call) in ("tool", "mcp")
                and (
                    index in nearest
                    or any(i in used and m.call == mock.call for i, m in enumerate(test.mocks))
                )
            ):
                # Said with the recorded notes: it names a call that happened, so it needs no
                # tools list to be true.
                recorded.append(
                    f"in test {test.name!r} the mock on {recorded_call(mock)} was not used; the "
                    "tool was called with other arguments"
                )
            elif labels.kind_of(mock.call) == "http":
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
            elif any(
                other.call == mock.call and other.args is None for other in test.mocks[:index]
            ):
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
    return notes, mistakes, recorded


def recorded_call(mock: Mock) -> str:
    """A recorded mock as a person finds it: its label, and its arguments or its request."""
    if labels.kind_of(mock.call) == "http":
        request = " ".join(part for part in (mock.method, mock.path) if part)
        return f"{mock.call} {request}".rstrip()
    if mock.args is None:
        return mock.call
    args = redact(json.dumps(mock.args, ensure_ascii=False))
    return f"{mock.call} with {args if len(args) <= PREVIEW else args[:PREVIEW] + '...'}"


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
    wanted = f"SCRIMMAGE_RUN={run}".encode()
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


def wait_run(proc: subprocess.Popen[bytes], seconds: float) -> bool:
    """Wait for the run's program to end. True when it is still going at its time limit.

    One function, so a test can make the time limit arrive at an event of its own.
    """
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return True
    return False


def stop_group(proc: subprocess.Popen[bytes]) -> None:
    """One run's group stopped; see stop_groups."""
    stop_groups([proc])


def stop_groups(procs: Sequence[subprocess.Popen[bytes]]) -> None:
    """SIGTERM to every run's process group at once, then SIGKILL to what is left after
    five seconds in all, not five seconds for each.

    It waits on the groups, not on the programs: a child can outlive the program, and can
    ignore SIGTERM after the program has gone.
    """
    waiting = [proc for proc in procs if signal_group(proc, signal.SIGTERM)]
    deadline = time.monotonic() + KILL_GRACE_S
    while waiting and time.monotonic() < deadline:
        waiting = [proc for proc in waiting if group_alive(proc)]
        if waiting:
            time.sleep(GROUP_POLL_S)
    for proc in waiting:
        signal_group(proc, signal.SIGKILL)
    for proc in procs:
        if proc.poll() is None:  # it left the group by itself
            proc.kill()
        proc.wait()


def group_alive(proc: subprocess.Popen[bytes]) -> bool:
    proc.poll()  # a program that has ended stays in the group until it is reaped
    return signal_group(proc, 0)


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


def broken_runs(report: RowReport) -> list[tuple[judge.Broken, list[RunResult]]]:
    """Each rule a run broke, with the runs that broke it, the most broken first."""
    tally: dict[judge.Broken, list[RunResult]] = {}
    for r in report.runs:
        for broken in r.judgement.broken:
            tally.setdefault(broken, []).append(r)
    return sorted(tally.items(), key=lambda item: -len(item[1]))


def rule_lines(report: RowReport, folder: Path) -> list[str]:
    """Which rules broke, in which runs. Empty when no run broke one."""
    most = broken_runs(report)
    if not most:
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
    return [
        head,
        *(
            f"  {plural(len(runs), 'run')}: {rule.text}: {where_to_read(rule, runs, folder)}"
            f"{miss_pointer(runs)}"
            for rule, runs in most
        ),
    ]


def miss_pointer(runs: list[RunResult]) -> str:
    """Said beside a broken rule when some of its runs had a mock miss, as a real call that a
    mock was meant to answer can change what the agent did."""
    n = sum(1 for r in runs if r.miss_count)
    return f" ({n} of these runs had a mock miss; see the warnings)" if n else ""


def counts(ended: Counter[str]) -> str:
    return ", ".join(f"{n} {outcome}" for outcome, n in sorted(ended.items()))


@dataclass(frozen=True)
class Report:
    """What scrimmage test found, once. The text and the JSON are two renderings of it."""

    config: Config
    results: list[RunResult]
    folder: Path
    rows: list[RowReport]
    table: list[list[str]]
    failures: list[str]  # which rules broke, in which runs: the lines under the table
    # (list, text) in the order the text prints them, the list being one of LISTS: those
    # printed above the table, as a line that must not scroll away, and those below it
    above: list[tuple[str, str]]
    messages: list[tuple[str, str]]
    spent: float
    stopped_by: str | None
    code: int
    misses: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    misses_left_out: int = 0

    @property
    def every_message(self) -> list[tuple[str, str]]:
        """Every message, in the order the text prints them, for the JSON's lists."""
        return [*self.above, *self.messages]


def build_report(
    config: Config,
    results: list[RunResult],
    folder: Path,
    runner: "Runner | None" = None,
    *,
    judged: bool = True,
) -> Report:
    """What a run of tests found. judged=False is for scrimmage record, whose one run is not a
    rate: a row with no result, a failed rule and a mock no run used are then not mistakes."""
    reports = row_reports(config, results)
    table = [HEAD, *(row_cells(row_json(entry, config.agent)) for entry in reports)]
    mistakes: list[str] = []
    notes: list[str] = []
    failures: list[str] = []
    code = OK
    for entry in reports:
        row, mine, where = entry.row, entry.runs, entry.where
        verdict = entry.result
        failures += rule_lines(entry, folder)
        if judged and verdict.verdict == judge.NO_RESULT and not entry.refused:
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
    unused, wrong, recorded = (
        unused_mocks(config, results) if judged and stopped_by is None else ([], [], [])
    )
    mistakes += wrong
    notes += recorded
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
    if stopped_by in BUDGET_REASONS:
        numbers = stop_numbers(results, stopped_by)
        line = (
            budget_line(stopped_by, numbers, config.agent)
            if numbers is not None
            else f"the budget of ${config.agent.max_cost_usd:g} stopped the test"
        )
        mistakes.append(f"{line}; rows without runs have no result")
    leaked = runner.ledger.leaked() if runner is not None else []
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
                    "working; check for a cache in front of the model or a client Scrimmage "
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
    grouped = miss_groups(results)
    web_misses = sorted(
        {
            f"{doc['call']} {doc['method']} {doc['path']}"
            for doc, _ in grouped
            if doc["reason"] == "request" and doc["action"] == "real"
        }
    )
    spent = sum(r.cost_usd for r in results)
    warnings: list[str] = []
    if budget_on and spent > (config.agent.max_cost_usd or 0):
        warnings.append(
            f"the test spent more than its budget of ${config.agent.max_cost_usd:g}, "
            "because the last calls cost more than was reserved for them"
        )
    warnings += [
        f"{hold.run} ended with {hold.label or 'a model call'} still holding "
        f"${hold.usd:.4f} of the budget, such as a call cut off when its run was killed; "
        "it stays held, as the call may have been sent"
        for hold in leaked
    ]
    if judged:
        warnings += [text for _, text in grouped[:MISS_LINES]]
        if len(grouped) > MISS_LINES:
            warnings.append(f"and {len(grouped) - MISS_LINES} more; see --json")
    else:
        warnings += [
            f"{call} ran for real, though the test has mocks for that host; "
            "none matched its method and path"
            for call in web_misses
        ]
    if hosted:
        warnings.append(
            f"{plural(hosted, 'model request')} offered provider tools that only read, "
            "such as web search; providers bill those per use, outside the token budget"
        )
    if silent:
        notes.append(f"{plural(len(silent), 'run')} answered with no model call")
    newer = sorted(
        {
            str(line.get("message"))
            for r in results
            for line in r.notices
            if line.get("notice") == "newer_version"
        }
    )
    messages = [
        *((WARNINGS, text) for text in warnings),
        *((NOTES, text) for text in notes),
        *((WARNINGS, text) for text in newer),
        *((MISTAKES, text) for text in dict.fromkeys(mistakes)),
    ]
    if code == INFRA:
        messages.append((INFRA_ERRORS, INFRA_LINE))
    elif mistakes:
        code = MISTAKE
    elif judged and any(r.result.verdict == judge.FAIL for r in reports):
        code = ROW_FAILED
    # The team's own statement that writes may run, so it is said first, not under the table.
    above = [(WARNINGS, TOOL_STAR_LINE)] if "tool:*" in config.agent.real else []
    return Report(
        config,
        results,
        folder,
        reports,
        table,
        failures,
        above,
        messages,
        spent,
        stopped_by,
        code,
        [doc for doc, _ in grouped[:MISS_JSON]],
        max(len(grouped) - MISS_JSON, 0),
    )


MISS_JSON, MISS_LINES = 200, 20


def miss_groups(results: list[RunResult]) -> list[tuple[dict[str, object], str]]:
    """The misses of every run, one group for the same call, evidence and nearest mock, as
    (the --json form, the warning line). A group counts distinct runs. Calls that ran for
    real come first, as nothing else reports them; a refused call is a mistake already."""
    total = Counter((r.row.test.name, r.row.name) for r in results)
    docs: dict[tuple[object, ...], dict[str, object]] = {}
    seen_in: dict[tuple[object, ...], set[str]] = {}
    for r in results:
        for m in r.misses:
            near = cast("dict[str, object] | None", m.get("nearest"))
            key = (
                r.row.test.name,
                r.row.name,
                m["call"],
                m["reason"],
                m["action"],
                m["seen"],
                m["method"],
                m["path"],
                None if near is None else near["mock"],
            )
            if key not in docs:
                shown = None if near is None else {**near, "mock": int(str(near["mock"])) + 1}
                docs[key] = {
                    "test": r.row.test.name,
                    "row": r.row.name,
                    "call": m["call"],
                    "reason": m["reason"],
                    "action": m["action"],
                    "method": m["method"],
                    "path": m["path"],
                    "seen": m["seen"],
                    "nearest": shown,
                    "run": r.run,
                }
                seen_in[key] = set()
            seen_in[key].add(r.run)
    out: list[tuple[dict[str, object], str]] = []
    for key, doc in docs.items():
        doc["runs"] = len(seen_in[key])
        where = (
            f"{doc['test']} {doc['row']}, {len(seen_in[key])} of "
            f"{total[str(doc['test']), str(doc['row'])]} runs"
        )
        out.append((doc, miss_text(doc, where)))
    out.sort(
        key=lambda x: (
            x[0]["action"] != "real",
            str(x[0]["test"]),
            str(x[0]["row"]),
            str(x[0]["call"]),
            str(x[0]["seen"] or ""),
        )
    )
    return out


def print_report(found: Report, out: TextIO) -> int:
    """The report as text: the table, the lines under it, and one line per message."""
    config = found.config
    print(
        f"scrimmage test: {plural(len(config.tests), 'test')}, {plural(len(found.rows), 'row')}, "
        f"{plural(len(found.results), 'run')}; "
        f"records in {found.folder}",
        file=out,
    )
    for kind, text in found.above:
        print(f"{PREFIXES[kind]}: {text}", file=out)
    for line in table_lines(found.table, [entry.where for entry in found.rows]):
        print(line, file=out)
    print("range: a 95% Wilson interval, from these runs alone", file=out)
    for line in found.failures:
        print(line, file=out)
    budget = config.agent.max_cost_usd
    limit = "no budget" if budget is None else f"a budget of ${budget:g}"
    print(f"model calls cost ${found.spent:.4f} of {limit}", file=out)
    for kind, text in found.messages:
        print(f"{PREFIXES[kind]}: {text}", file=out)
    return found.code


def report(
    config: Config,
    results: list[RunResult],
    folder: Path,
    out: TextIO,
    runner: "Runner | None" = None,
) -> int:
    return print_report(build_report(config, results, folder, runner), out)


def report_json(found: Report) -> dict[str, object]:
    """The fields `scrimmage test --json` adds to the envelope. Its lists come from messages."""
    return {
        "records": str(found.folder),
        "tests": len(found.config.tests),
        "rows": [row_json(entry, found.config.agent) for entry in found.rows],
        "cost_usd": round(found.spent, 8),
        "budget_usd": found.config.agent.max_cost_usd,
        "stopped_by": found.stopped_by,
        "mock_misses": found.misses,
        "mock_misses_left_out": found.misses_left_out,
    }


def fault_json(fault: Fault | None) -> dict[str, object] | None:
    """A row's fault, with the rules that judge the row in place of the test's, or null."""
    if fault is None:
        return None
    return {
        "call": fault.call,
        "do": fault.do,
        "when": fault.when,
        "model": fault.model,
        "method": fault.method,
        "path": fault.path,
        "raises": fault.raises,
        "expect": None if fault.expect is None else [list(item) for item in fault.expect],
        "forbid": None if fault.forbid is None else list(fault.forbid),
        "pass_rate": fault.pass_rate,
    }


def row_json(entry: RowReport, agent: Agent) -> dict[str, object]:
    """One table row, by the table's own column names, with its runs and broken rules."""
    row, mine, result = entry.row, entry.runs, entry.result
    fault = row.fault
    rate: float | None = None
    span: list[float] | None = None
    if result.judged:
        low, high = judge.wilson(result.passed, result.judged)
        rate, span = round(result.passed / result.judged, 4), [round(low, 4), round(high, 4)]
    return {
        "test": row.test.name,
        "row": row.name,
        "fault": fault_json(fault),
        "runs": len(mine),
        "fired": None if fault is None else sum(bool(r.fault_fired) for r in mine),
        "done": sum(1 for r in mine if r.outcome == "answered"),
        "passed": result.passed,
        "judged": result.judged,
        "rate": rate,
        "range": span,
        "pass_rate": row.rules.pass_rate,
        "tools": round(sum(r.tool_calls for r in mine) / max(len(mine), 1), 4),
        "cost_usd": round(sum(r.cost_usd for r in mine), 8),
        "result": result.verdict,
        "broken": [
            {"kind": rule.kind, "text": rule.text, "runs": [r.run for r in runs]}
            for rule, runs in broken_runs(entry)
        ],
        "mock_miss_count": sum(r.miss_count for r in mine),
        "run_details": [run_json(r) for r in mine],
        "test_sha256": row_hash(row.test, fault, agent),
        "test_sha256_v": HASH_VERSION,
    }


def run_json(result: RunResult) -> dict[str, object]:
    answer = None if result.answer is None else redact(result.answer)[:PREVIEW]
    return {
        "run": result.run,
        "outcome": result.outcome,
        "fault_fired": result.fault_fired,
        "judged": result.judgement.verdict,
        "left_out": result.judgement.left_out,
        "cost_usd": round(result.cost_usd, 8),
        "answer_preview": answer,
        "mock_miss_count": result.miss_count,
    }


def budget_line(reason: str, numbers: dict[str, object], agent: Agent) -> str:
    """Why the budget stopped the test, with the fix that fits. "Reached" only when money
    spent reached the limit: a call can be refused while most of the budget is only held."""
    usd, spent, held, limit, waited = (
        float(str(numbers.get(key, 0))) for key in ("usd", "spent", "held", "limit", "waited_s")
    )
    if spent >= limit:
        return f"the budget of ${limit:g} was reached: ${spent:.4f} was spent, so the test stopped"
    wait = f", after a wait of {waited:.0f}s for calls in flight" if reason == "budget_held" else ""
    holds = [planner.obj(h) for h in cast(list[object], numbers.get("holds") or [])]
    who = ", ".join(
        f"{h.get('label')} in {h.get('run')} ${planner.number(h, 'usd'):.4f}"
        + (f", left by {h.get('run')}, which ended while still holding" if h.get("ended") else "")
        for h in holds
    )
    text = (
        f"a model call could not be reserved: it could cost up to ${usd:.4f}, ${spent:.4f} was "
        f"spent and ${held:.4f} was held by calls in flight{f' ({who})' if who else ''}, of a "
        f"budget of ${limit:g}{wait}, so the test stopped"
    )
    hold_less = (
        f"set max_tokens on the call or lower [agent] reserve_output_tokens "
        f"({agent.reserve_output_tokens}) to hold less"
    )
    if any(h.get("ended") for h in holds):
        return f"{text}; a run killed during a call left its hold, which never comes back: " + (
            "see that run's log, or raise [agent] max_cost_usd"
        )
    if numbers.get("own") is True:
        return f"{text}; the run's own calls held the room, so close a stream before the next " + (
            f"call, or {hold_less}"
        )
    fewer = ", lower [agent] parallel" if agent.parallel > 1 else ""
    return f"{text}; {hold_less}{fewer}, or raise [agent] max_cost_usd"


def budget_refusal(lines: list[dict[str, object]]) -> dict[str, object] | None:
    """The run's first refusal for the budget, even if another refusal came first."""
    return next((x for x in lines if "budget" in x and x.get("reason") in BUDGET_REASONS), None)


def stop_numbers(results: list[RunResult], stopped_by: str) -> dict[str, object] | None:
    """The numbers of the refusal that stopped the test, not another run's, refused for
    another reason while the test was being stopped."""
    return next((r.budget for r in results if r.budget_reason == stopped_by and r.budget), None)


def reached(numbers: dict[str, object]) -> bool:
    """Money spent reached the limit, as the refusal saw it."""
    return float(str(numbers.get("spent", 0))) >= float(str(numbers.get("limit", 0)))
