"""The runner: one new program per run, the run table, exit codes.

Exit codes: 0 when every row passed; 1 when a row failed; 2 for a mistake in the test, a
refused call and a test the budget stopped included; 3 for an infrastructure error. The
higher code wins, as a mistake or an error makes the rates not worth trusting.
"""

import contextlib
import dataclasses
import errno
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from scrimmage import judge, labels, protocol, render, service_client
from scrimmage import plan as planner
from scrimmage.budget import Ledger
from scrimmage.config import (
    REPLY_FAULTS,
    Agent,
    Config,
    Expanded,
    Fault,
    Mock,
    Test,
    as_written,
    carried_model_warnings,
    command_args,
    real_writes,
    service_url,
    write_warnings,
)
from scrimmage.control import ControlServer, proc_key
from scrimmage.decide import MISTAKE_REFUSAL
from scrimmage.managed import START_TRIES, CopyFailed, Managed, free_port
from scrimmage.outcomes import (  # the runner's names for them
    INFRA,
    INFRA_ERROR,
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
from scrimmage.records import RECORD_VERSION, RecordStore
from scrimmage.redact import redact, redact_decoded
from scrimmage.render import HEAD, miss_text, row_cells, table_lines
from scrimmage.results import HASH_VERSION, STOPPED_BY_SIGNAL, row_hash
from scrimmage.state import COMMAND_CAP, now_iso

END = b"\x00"  # ends the stop watcher; no signal has the number 0
TAIL_BYTES = 1024 * 1024  # how much of a script's output is read to find its last line
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
PROBE_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")  # a client builds only with a key set
KILL_GRACE_S = 5.0
GROUP_POLL_S = 0.01
KILLED_S = 1.0  # how long a group SIGKILLed may take to be gone
# A refusal for one of these reasons stops the whole test, not only its row.
STOP_TEST = (
    "budget",
    "budget_held",
    "no_price",
    "unknown_tool_type",
    "unmetered_tool",
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
# Outcomes of a run that ended with no answer to judge: a test whose runs all ended so shows
# nothing, so its unused mocks are not reported.
FAILED = ("crashed", "timed out", "refused call", "budget reached", "not reserved")
# A baseline whose every run ended so cannot be compared with, so its fault rows are not run: a
# provider's 400 or 404 would only be paid for again, as a refused call would be.
BASELINE_FAILED_OUTCOMES = (*FAILED, INFRA_ERROR)
BUDGET_REASONS = ("budget", "budget_held")  # refusals for the budget
SHOWN_RUNS = 3  # how many runs a line names for one broken rule
SHOWN_NAMES = 5  # how many model names a line lists
# The lists a report's messages go in, and the word each line starts with in the text.
PREFIXES = {
    MISTAKES: "test mistake",
    NOTES: "note",
    WARNINGS: "warning",
    INFRA_ERRORS: INFRA_ERROR,
}
TOOL_STAR_LINE = 'every tool that is not mocked runs for real (real = ["tool:*"])'
# Said in the text and in --json alike, so it names a field both can show.
INFRA_LINE = (
    "see each run whose outcome is infrastructure error, and its .log file in the records folder"
)
NOT_JUDGED = judge.RunJudgement(left_out="not judged")


def context_lost(lines: list[dict[str, object]]) -> bool:
    """A service run whose record cannot be known whole: an orphan's refusal, or a call the
    gate saw outside any request while the run was open, which could have been its own."""
    return any(
        line.get("reason") == "orphan" or line.get("notice") == "lost_context" for line in lines
    )


def outside_entries(lines: list[dict[str, object]]) -> list[str]:
    """Each call the gate saw outside any request while the run was open, as the entry to
    add to [agent] background_real, in the order they came, each once."""
    found = [
        str(line["background_entry"])
        for line in lines
        if "background_entry" in line and line.get("notice") != "background"
    ]
    return list(dict.fromkeys(found))


def is_exercised(fired: bool | None, outcome: str) -> bool:
    """A run tested its fault: it fired (or the row has none), or the test was stopped."""
    return fired is not False or outcome == "stopped"


def fault_fired(has_fault: bool, fault_lines: int) -> bool | None:
    """None on a baseline, else whether the gate injected the fault at least once."""
    return fault_lines > 0 if has_fault else None


def chose_refused(has_fault: bool, first_refusal: str) -> bool:
    """The agent chose a call the test does not allow, under a fault."""
    return has_fault and first_refusal in CHOSEN_CALLS


def ends_its_row(has_fault: bool, first_refusal: str, by_config: bool) -> bool:
    """Whether a run's first refusal stops the rest of its row. A call the agent chose under
    a fault does not, so the row counts every run; a refusal the test's setup caused does,
    whatever its reason, as every run would meet it again."""
    return by_config or not chose_refused(has_fault, first_refusal)


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
    # Why each call a carried fault could not change was left as it was, one entry per call.
    unchanged: list[str] = field(default_factory=list[str])
    # Calls that ran for real in an append or replace row and could be writes, by name.
    writes: dict[str, int] = field(default_factory=dict[str, int])
    # A fault changed a result and no model request after it carried the text: a heuristic.
    undelivered: bool = False
    # The control load of the process that served the run, from its last batch.
    control_busy: int | None = None
    control_p99_ms: float | None = None

    @property
    def miss_count(self) -> int:
        return len(self.misses)

    @property
    def exercised(self) -> bool:
        return is_exercised(self.fault_fired, self.outcome)


@dataclass
class Ended:
    """How a run ended, before its record is read: a program's exit or a service's reply."""

    answer: str | None
    timed_out: bool
    ok: bool  # the program exited 0, or the service answered 2xx with an answer
    kept_out: list[dict[str, object]]
    left: list[tuple[int, str]]
    infra: bool = False  # the gate's own 503, or the connection failed
    mistakes: list[str] = field(default_factory=list[str])


PLAN_CHANGED = "the process that served this run used another plan"
READY_S = 30.0  # how long the probe asks a service that does not listen yet
READY_STEP_S = 0.2
CONTROL_STEP_S = 1.0  # how often the probe asks again while the service cannot reach the runner
WAITERS = 64  # service runs whose record waits at once: the control server's connections


REFUSED_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTDIR, errno.ENOENT)


def records_folder(path: Path) -> bool:
    """Make --records DIR, or check the one that is there. True when this call made it.

    A folder that exists is used as it is: it is the user's own and no wider than 0700, or the
    test stops. A mistake in the path is exit 2, so a CI job does not retry it; a full disk or a
    failing one stays an infrastructure error.
    """
    try:
        found = path.stat()
    except FileNotFoundError:
        found = None
    except OSError as exc:
        raise service_client.Setup(2, f"--records {path}: {exc.strerror}") from None
    if found is None:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)  # records hold customer data: only their owner may list them
        except OSError as exc:
            if exc.errno not in REFUSED_ERRNOS:
                raise
            raise service_client.Setup(2, f"--records {path}: {exc.strerror}") from None
        return True
    if not stat.S_ISDIR(found.st_mode):
        raise service_client.Setup(2, f"--records {path} is not a folder")
    inside = path / "scrimmage-runs"
    if found.st_uid != os.getuid():
        raise service_client.Setup(
            2,
            f"--records {path} is owned by uid {found.st_uid}; point it at a folder inside it, "
            f"such as {inside}",
        )
    if found.st_mode & 0o077:
        raise service_client.Setup(
            2,
            f"--records {path} has mode {stat.S_IMODE(found.st_mode):o}, which lets others in; "
            f"run chmod 700 on it, or point it at a new folder inside it, such as {inside}",
        )
    return False


def settled(found: "Iterable[RunResult | Future[RunResult] | None]") -> list[RunResult | None]:
    """The results of runs, each service run's once its record wait has ended."""
    return [r.result() if isinstance(r, Future) else r for r in list(found)]


class Runner:
    def __init__(
        self,
        config: Config,
        control_url: Callable[[str], str] = lambda url: url,
        stop_flag: list[str] | None = None,
        *,
        record: bool = False,
        probe: bool = False,
        scratch: bool = False,
        records: Path | None = None,
    ) -> None:
        """scratch: the records go to a folder of their own to remove, as scrimmage check
        --service writes none beside the file.
        records: scrimmage test --records, the folder to hold the test's folder, in place of
        scrimmage-runs beside the file."""
        port = 0
        if config.agent.start:
            port = free_port()
            config = on_port(config, port)
        self.config = config
        self.record = record  # scrimmage record: real calls' lines carry what a mock needs
        self.probe = probe  # scrimmage init --probe: the first model request ends the run
        self.control_url = control_url  # lets a test point the agent elsewhere
        # Read first: these can stop the test, and nothing is made on disk until they pass.
        self.token = secrets.token_hex(32)
        address = ("127.0.0.1", 0)
        tls_context = None
        if config.agent.service and not config.agent.start:
            # A service was started before this runner, with its token and control address:
            # the runner reads the token from its own environment, and the address from the
            # file's control setting or, for a service on this machine, the same environment.
            self.token = service_client.runner_token()
            address = service_client.control_address(config.agent)
            tls_context = service_client.server_tls(config.agent, config.root)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        runs = Path(tempfile.mkdtemp(prefix="scrimmage-check-")) if scratch else None
        # Only a folder this run makes is changed: an existing --records folder, such as the
        # config folder, is left as it is, with no mode change and no .gitignore.
        made = True
        if records is not None:
            runs = records.absolute()
            made = records_folder(runs)
        else:
            runs = runs or config.root / "scrimmage-runs"
            runs.mkdir(mode=0o700, exist_ok=True)
            os.chmod(runs, 0o700)  # records hold customer data: only their owner may list them
        self.store = new_store(runs, stamp)
        self.store.scrub = config.scrub
        self.folder = self.store.folder
        # Records can hold customer data, so they stay out of the team's git repo.
        ignore = runs / ".gitignore"
        if made and not ignore.exists():
            ignore.write_text("*\n")
        # The values ${NAME} filled in: this test's own, removed from what the runner writes.
        self.scrub = config.scrub
        self.ledger = Ledger(config.agent.max_cost_usd)
        try:
            self.server = ControlServer(
                self.token,
                self.store,
                on_refused=self._refused,
                ledger=self.ledger,
                address=address,
                tls=tls_context,
            )
        except OSError as exc:  # the service's control port is held by another process
            if scratch:
                shutil.rmtree(runs, ignore_errors=True)
            raise service_client.Setup(
                3, f"the control address {address[0]}:{address[1]} cannot be bound: {exc.strerror}"
            ) from None
        # The copy of the service the runner starts and stops, with start; its token is ours.
        self.managed = (
            Managed(config, port, self.token, self.store, keep_log=not scratch)
            if config.agent.start
            else None
        )
        self.answers = Path(tempfile.mkdtemp(prefix="scrimmage-answers-"))
        self.lock = threading.Lock()
        self.sent = 0  # service requests sent: the setup request and every run
        self.setup_spent = 0.0  # what the setup request's model call cost: in no row, in the total
        self.ended = False  # run_all has read and cleared stop_flag; the watcher adds nothing
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.stopped_rows: set[tuple[str, str]] = set()
        self.row_of: dict[str, planner.Row] = {}
        self.cancelled = False
        self.turned_away = 0  # runs one() did not start because the test was cancelled
        # A service run's record wait, which runs beside the next runs: each waits on an event,
        # so a thread each costs nothing while it waits. Made by run_all, for a service only.
        self.waiters: ThreadPoolExecutor | None = None
        # One turn per row of a service test: its runs go one after another.
        self.turns: dict[tuple[str, str], threading.Semaphore] = {}
        self.tools_seen: list[str] | None = None  # as the agent's process reported them
        self.asked_s = 0.0  # how long the probe asked again while the ping failed
        self.went_silent = False  # the service stopped answering the probe in that wait
        self.control_ping: dict[str, object] | None = None  # the service's ping, as it reported
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
        """The first refusal ends its run and its row; some reasons end the whole test. A call
        the agent chose under a fault ends its run only: the row's other runs still run, so
        its rate counts every run, such as an attack the agent obeyed in 8 of 10."""
        with self.lock:
            row = self.row_of[run]
            known = self.server.runs.get(run)
            setup = known is not None and known.setup_refused
            if ends_its_row(row.fault is not None, reason, setup):
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
        # Nothing reaches a model, so a key is needed only to build the client. A key the agent
        # already has picks its client, so a placeholder is set only when it has none.
        if self.probe and not any(env.get(name) for name in PROBE_KEYS):
            for name in PROBE_KEYS:
                env[name] = "scrimmage-probe-sends-no-request"
        return env

    def program(self, question: str) -> list[str]:
        """What one run starts. A script's command was split when the file was read, so the
        question stays one argument, and no shell ever sees it."""
        agent = self.config.agent
        if agent.command:
            return command_args(agent, question)
        answer = [agent.answer] if agent.answer else []
        return [sys.executable, "-P", "-m", "scrimmage.start", agent.function, question, *answer]

    def one(self, row: planner.Row) -> "RunResult | Future[RunResult] | None":
        if self.config.agent.service:
            # A row's runs go one after another, each once the one before it is settled, so a
            # refusal stops its row before the next run is sent. Other rows go beside it.
            turn = self.row_turn(row)
            turn.acquire()
            try:
                found = self.one_now(row)
            except BaseException:
                turn.release()
                raise
            if isinstance(found, Future):
                found.add_done_callback(lambda _: turn.release())
            else:
                turn.release()
            return found
        return self.one_now(row)

    def in_turns(self, rows: list[planner.Row], runs: int) -> list[planner.Row]:
        """Each row's runs, in order. A service's rows take turns, one run of each in each
        round, so the parallel slots go to other rows while a row's run settles."""
        if self.config.agent.service:
            return [row for _ in range(runs) for row in rows]
        return [row for row in rows for _ in range(runs)]

    def row_turn(self, row: planner.Row) -> threading.Semaphore:
        with self.lock:
            return self.turns.setdefault((row.test.name, row.name), threading.Semaphore(1))

    def one_now(self, row: planner.Row) -> "RunResult | Future[RunResult] | None":
        with self.lock:
            if self.cancelled:
                self.turned_away += 1
                return None
            if (row.test.name, row.name) in self.stopped_rows:
                return None
            run = f"run-{secrets.token_hex(6)}"
            self.row_of[run] = row
        plan = planner.plan_for(self.config, row, run)
        if self.record:
            plan = dataclasses.replace(plan, record=True)
        if self.probe:
            plan = dataclasses.replace(plan, probe=True)
        service = bool(self.config.agent.service)
        self.server.open_run(run, row.name, plan.to_json(), service=service)
        if self.cancelled:
            self.server.close_run(run)
            with self.lock:
                self.turned_away += 1
            return None
        if service:
            return self.served(run, row)
        command = [
            sys.executable,
            "-P",
            "-m",
            "scrimmage.run_command",
            *self.program(row.test.question),
        ]
        # A script's last printed line can be its answer, so what it prints stays apart.
        log = owner_only(self.folder / f"{run}.log")
        out = owner_only(self.folder / f"{run}.out") if self.config.agent.command else log
        try:
            # run_command asks Linux to kill the run when this thread exits, so runs must be
            # started from a thread that outlives them: the pool's, which lives through run_all.
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
        try:
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
            # The server stops a test on a thread of its own, which this run's end can
            # overtake. Settle it here, so the next run sees it.
            if state.broken or state.refusals or kept_out:
                reason = "other_python" if kept_out else (state.refusals or ["structure"])[0]
                self._refused(run, "structure" if state.broken else reason)
            answer = self.answer_of(run, proc.returncode)  # read first: judged as it was
        finally:
            for written in ("log", "out"):  # even when the run's end failed
                self.store.scrub_file(self.folder / f"{run}.{written}")
        ended = Ended(answer, timed_out, proc.returncode == 0, kept_out, left)
        result = self.concluded(run, row, state, ended)
        self.check_labels(result)
        return result

    def concluded(self, run: str, row: planner.Row, state: Any, ended: "Ended") -> RunResult:
        """A run's outcome, result and summary, from its record and how it ended: a program
        that exited, or a service's reply."""
        lines = self.store.lines(run)
        answer, timed_out, kept_out, left = (
            ended.answer,
            ended.timed_out,
            ended.kept_out,
            ended.left,
        )
        # Another run stopped the test and this run was stopped with it. That says
        # nothing about this run, and it never got to send its last batch.
        stopped = (
            self.cancelled and not timed_out and not state.refused and not state.broken
        ) and not ended.ok
        ended_itself = not timed_out and not state.refused and not stopped
        # Every process that ended by itself sent a last batch saying how many lines it
        # made. A missing batch, or fewer lines received from a process, is a gap.
        per_proc = Counter(proc_key(line.get("proc")) for line in lines)
        gap = any(per_proc[proc] < made for proc, made in state.made.items())
        unfinished = not state.made or any(proc not in state.made for proc in per_proc)
        incomplete = bool(state.dropped) or (ended_itself and (gap or unfinished))
        own_digest = protocol.plan_digest(self.server.token, state.plan) if state.service else None
        # Equal digests say nothing more. Another says the claiming process used another plan.
        # The control server refuses a claimed run's last batch with no digest.
        claimed = state.claimed_plan_digest
        changed = claimed is not None and claimed != own_digest
        made = kept_out + [self.left_line(run, row, *entry) for entry in left]
        if changed:
            made.append(self.runner_notice(run, row, "plan_changed", PLAN_CHANGED))
        self.store.add(run, made)
        lines += made
        model = [line for line in lines if line.get("kind") == "model"]
        notices = [line for line in lines if "notice" in line]
        cost = sum(float(str(line.get("cost_usd") or 0)) for line in model)
        # The provider failing by itself says nothing about the agent.
        real_errors = [
            e for e in (real_error(x, model, self.scrub) for x in model) if e is not None
        ]
        broken = any(n.get("notice") == "structure" for n in notices)
        # No plan, or a call made outside the run's context: says nothing about the agent.
        no_plan = "no_plan" in state.refusals or context_lost(lines)
        # A gate's refusal for a mistake in the test is not a call the agent made or chose.
        chosen = [r for r in state.refusals if r != MISTAKE_REFUSAL]
        first = (chosen or [""])[0]
        refusal = budget_refusal(lines)
        numbers = None if refusal is None else cast(dict[str, object], refusal["budget"])
        if stopped:
            outcome = "stopped"
        elif (
            not state.fetched
            or incomplete
            or real_errors
            or broken
            or no_plan
            or ended.infra
            or changed
        ):
            outcome = INFRA_ERROR
        elif first in BUDGET_REASONS:
            outcome = "budget reached" if numbers and reached(numbers) else "not reserved"
        elif chosen or kept_out:
            outcome = "refused call"
        elif timed_out:
            outcome = "timed out"
        elif ended.ok and answer is not None:
            outcome = "answered"
        else:
            outcome = "crashed"
        calls = {
            a: sum(1 for line in lines if line.get("action") == a)
            for a in ("real", "mock", "fault", "refused")
        }
        # How many calls had their real code or request run beneath a carried fault.
        calls["under_real"] = sum(1 for line in lines if line.get("under") == "real")
        fired = fault_fired(row.fault is not None, calls["fault"])
        mistakes = [str(line["mistake"]) for line in lines if "mistake" in line] + ended.mistakes
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
            mocked=used_mocks(lines),
            unchanged=[
                str(cast(dict[str, object], line["unchanged"])["why"])
                for line in lines
                if "unchanged" in line
            ],
            writes=self.real_writes_in(row, lines),
            # A changed model reply is read by the agent, not sent on to a model.
            undelivered=any("changed" in line and line.get("kind") != "model" for line in lines)
            and not any(line.get("carries_injected") for line in model),
            tools_seen=reported_tools(notices),
            misses=misses,
            tool_calls=sum(1 for line in lines if line.get("kind") in ("tool", "mcp")),
            models={(str(line["call"]), str(line.get("model") or "")) for line in model},
            budget=numbers,
            budget_reason="" if refusal is None else str(refusal["reason"]),
        )
        # Here the raw answer is still at hand. The record only ever gets the redacted one.
        chose = chose_refused(row.fault is not None, first)
        refused = [
            str(line["call"])
            for line in lines
            if line.get("action") == "refused" and line.get("reason") != MISTAKE_REFUSAL
        ]
        result.judgement = judge.judge_run(
            outcome,
            answer,
            result.exercised,
            row.rules,
            bool(mistakes),
            refused[0] if chose and outcome == "refused call" else None,
        )
        summary: dict[str, object] = {
            "answer": None if answer is None else redact_decoded(answer, self.scrub),
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
        if state.service:
            summary.update(
                plan_digest=own_digest,
                claimed_plan_digest=claimed,
                claimed_by=state.claimer,
            )
        # The claiming process's control load in this run's window, from its last batch.
        result.control_busy, result.control_p99_ms = state.control_busy, state.control_p99_ms
        if state.control_busy is not None:
            summary.update(control_busy=state.control_busy, control_p99_ms=state.control_p99_ms)
        self.store.close_run(run, row.test.name, row.name, summary)
        return result

    def send(self, header: str, question: str) -> service_client.Reply:
        """One request to the service, counted: a control server that no SDK reaches shows as
        requests sent and no claims received."""
        with self.lock:
            self.sent += 1
        agent = self.config.agent
        return service_client.run_request(agent, header, question, agent.run_timeout)

    def served(self, run: str, row: planner.Row) -> "RunResult | Future[RunResult]":
        """A service run: one POST. The parallel slot is this thread, and it frees at the
        reply: the wait for the record, and the result, run on a waiter beside the next
        runs. A reply that stops the test is settled here, before the next run."""
        agent = self.config.agent
        header = f"{run}.{protocol.run_mac(self.token, run)}"
        reply = self.send(header, row.test.question)
        replied = time.monotonic()  # the record's wait counts from here, not from its waiter
        ok = reply.status is not None and 200 <= reply.status < 300
        gate = reply.gate_refused
        streamed = ok and service_client.streamed(reply, agent)
        unclaimed = not self.server.runs[run].fetched and reply.status is not None and gate is None
        wrong_version = gate == "version"
        clock_off = gate == "clock"  # every later claim of this process would be refused too
        # The copy the runner started has exited: every later run would fail the same way. A
        # test already stopping, as on Ctrl-C, stopped it itself.
        dead = (
            reply.failed is not None
            and self.managed is not None
            and not self.cancelled
            and self.managed.gone()
        )
        if unclaimed or streamed or wrong_version or clock_off or dead:
            with self.lock:
                self.cancelled = True  # under this lock, so no run starts from here on
        assert self.waiters is not None  # run_all made it, as this is a service
        # A row's next run waits for this one, so the queue holds at most one wait per row.
        later = self.waiters.submit(self.served_result, run, row, reply, streamed, replied)
        if dead:
            later.result()
            assert self.managed is not None
            raise self.managed.exited("during the test, so no further run was sent")
        if unclaimed:
            later.result()
            # A process answered without Scrimmage, such as a worker started another way:
            # every later run could be the same, so none is sent.
            raise service_client.Setup(
                3,
                f"{run} was answered with {reply.status} but its plan was never claimed, so a "
                "process without Scrimmage served it; no further run was sent",
            )
        if wrong_version:
            later.result()
            raise self.version_refusal(run)
        if clock_off:
            later.result()
            raise service_client.Setup(3, f"{run} was refused: {service_client.CLOCK_OFF}")
        if streamed:
            later.result()
            raise service_client.Setup(
                2,
                f"{run}: the service streamed its reply ({reply.headers.get('content-type')}), "
                "which this version does not judge; one run is one JSON or text reply",
            )
        return later

    def version_refusal(self, run: str) -> "service_client.Setup":
        """A worker of the service of another version refused its claim: every run it gets
        would fail the same way, so the test stops."""
        found = self.server.refused_sdk
        said = service_client.other_version(
            found[0] if found else None, "in the service, in every process of it"
        )
        return service_client.Setup(2, f"{run} was refused: {said}")

    def served_result(
        self,
        run: str,
        row: planner.Row,
        reply: service_client.Reply,
        streamed: bool,
        replied: float,
    ) -> RunResult:
        """A service run's record, once its last batch came or the wait ended, and its result."""
        agent = self.config.agent
        state = self.wait_record(run, replied)
        ok = reply.status is not None and 200 <= reply.status < 300
        answer, infra = None, reply.failed is not None
        mistakes: list[str] = []
        gate = reply.gate_refused
        if gate is not None:
            infra = True
            said = service_client.CLOCK_OFF if gate == "clock" else None
            mistakes.append(said or f"the service's gate answered 503 {gate}")
        elif ok and not streamed:
            answer, wrong = service_client.answer_of(reply, agent)
            if wrong is not None:
                mistakes.append(wrong)
        if state.broken or state.refusals:
            self._refused(run, "structure" if state.broken else state.refusals[0])
        ended = Ended(answer, reply.timed_out, ok and answer is not None, [], [], infra, mistakes)
        result = self.concluded(run, row, state, ended)
        self.check_labels(result)
        return result

    def conclude_setup(
        self, run: str, row: planner.Row, state: Any, reply: service_client.Reply
    ) -> float:
        """The setup request's summary, in the same shape as a run's, and what its model
        calls cost. Its labels are not checked here: the first run does that, as before."""
        agent = self.config.agent
        ok = reply.status is not None and 200 <= reply.status < 300
        answer = None
        if ok and not service_client.streamed(reply, agent):
            answer = service_client.answer_of(reply, agent)[0]
        infra = reply.failed is not None or reply.gate_refused is not None
        ended = Ended(answer, reply.timed_out, answer is not None, [], [], infra)
        return self.concluded(run, row, state, ended).cost_usd

    def probe_service(self) -> service_client.Probed:
        """The probe, asked again while the service starts: exit 3 when no gated service
        answers, or the service's clock and this machine's differ by more than the window.

        With start, a probe that fails first asks whether the copy has exited: one whose port
        another process took exits, and that process may be what answered the probe. Such a
        copy is started again on a new port, up to START_TRIES starts; one that failed in any
        other way is not."""
        while True:
            try:
                return self.probe_until_ready()
            except CopyFailed:
                managed = self.managed
                if self.stop_flag:  # Ctrl-C or SIGTERM, which stopped the copy too
                    raise Interrupted(self.stop_flag[0]) from None
                if managed is None or len(managed.ports) >= START_TRIES:
                    raise
                if not managed.port_taken():
                    raise
                self.start_copy_again(managed)

    def start_copy_again(self, managed: Managed) -> None:
        """The copy lost its port to another process before it bound it: a new port, in the
        address every request goes to and in the copy's command, and a new start."""
        taken, port = managed.port, free_port()
        config = on_port(self.config, port)
        if not managed.start_again(port, config, lambda: bool(self.stop_flag)):  # a stop
            raise Interrupted(self.stop_flag[0] if self.stop_flag else "SIGINT")
        self.config = config
        print(
            f"scrimmage: port {taken} was taken before the service bound it; started it "
            f"again on port {port}, start {len(managed.ports)} of {START_TRIES}",
            file=sys.stderr,
            flush=True,
        )

    def probe_until_ready(self) -> service_client.Probed:
        until = time.monotonic() + READY_S
        try:
            return self.probe_ready()
        except service_client.Setup as exc:
            if self.stop_flag:  # Ctrl-C or SIGTERM, which stopped the copy too
                raise Interrupted(self.stop_flag[0]) from None
            if isinstance(exc, CopyFailed):
                raise
            if self.managed is not None and self.managed.lost_its_port(until):
                if self.stop_flag:  # Ctrl-C or SIGTERM stopped the copy
                    raise Interrupted(self.stop_flag[0]) from None
                raise self.managed.exited("before it was ready") from None
            raise

    def copy_alive(self) -> Callable[[], bool] | None:
        """What a probe asks while it waits: whether the copy started with [agent] start still
        runs, so a process that took its port and never answers holds the probe no longer."""
        return self.managed.alive if self.managed is not None else None

    def probe_ready(self) -> service_client.Probed:
        agent = self.config.agent
        _, _, path = service_client.target(agent)
        deadline = time.monotonic() + READY_S
        probed = service_client.probe(agent, self.token, self.copy_alive())
        reply = probed.reply

        def starting(found: service_client.Reply) -> bool:
            """No reply yet: refused, timed out, or taken and cut by a published port. A reply
            whose headers were read ends the wait, so a reset after it is final."""
            return service_client.not_listening(found) or service_client.accepted_then_cut(found)

        if starting(reply):
            host, port, _ = service_client.target(agent)
            print(f"scrimmage: waiting for {host}:{port} to listen", file=sys.stderr, flush=True)
        # A service started just before the test may not listen yet: ask again until it does.
        while starting(reply) and time.monotonic() < deadline:
            if self.cancelled or self.stop_flag:  # Ctrl-C or SIGTERM ends the wait
                raise Interrupted(self.stop_flag[0] if self.stop_flag else "SIGINT")
            if self.managed is not None and not self.managed.alive():
                raise self.managed.exited("before it was ready")
            time.sleep(READY_STEP_S)  # nothing tells a closed port it has opened
            probed = service_client.probe(agent, self.token, self.copy_alive())
            reply = probed.reply
        if self.managed is not None and starting(reply):
            if not self.managed.alive():
                raise self.managed.exited("before it was ready")
            raise self.managed.failed(f"did not answer a probe in {READY_S:g} s")
        if service_client.not_listening(reply) and str(reply.failed).startswith("TimeoutError"):
            raise service_client.Setup(
                3,
                f"the connection to the service's address for POST {path} timed out, after "
                f"{READY_S:g} s of asking (a firewall may drop it): the service is not reachable "
                "from here",
            )
        if service_client.not_listening(reply):
            raise service_client.Setup(
                3,
                f"nothing listens at the service's address for POST {path}, after {READY_S:g} s "
                f"of asking ({reply.failed}): the service is not started, is still starting, "
                "or listens at another address",
            )
        if not reply.connected and not reply.unanswered:
            raise service_client.Setup(
                3,
                f"cannot reach the service's address for POST {path} ({reply.failed}): no such "
                "host, or no route to it",
            )
        if reply.too_long:
            raise service_client.Setup(
                3,
                f"the service at POST {path} answered the probe with more than "
                f"{service_client.PROBE_MAX // (1024 * 1024)} MiB; Scrimmage's startup report "
                "is a few KB, so something other than Scrimmage answered",
            )
        if reply.timed_out:
            raise service_client.Setup(
                3,
                f"the service at POST {path} did not finish its reply to the probe within "
                f"{service_client.PROBE_S:g} s",
            )
        if reply.failed is not None:
            hint = (
                "; a container's published port can accept a connection before the app "
                "inside listens, and this one was still reset or closed with no reply after "
                f"{READY_S:g} s"
                if starting(reply)
                else ""
            )
            raise service_client.Setup(
                3,
                f"the service at POST {path} took the probe's connection but gave no reply: "
                f"{reply.failed}{hint}",
            )
        if probed.clock_refused:
            raise service_client.Setup(3, service_client.CLOCK_OFF)
        report = probed.report
        if report is None or not {"asgi", "starlette"} & set(
            cast(list[str], report.get("gates") or [])
        ):
            raise service_client.Setup(
                3,
                f"x-scrimmage-run does not reach Scrimmage in the service at POST {path}: a proxy "
                "dropped the header, the service was not started with scrimmage-run, or with "
                "PYTHONPATH beginning with the folder scrimmage path prints, and "
                "SCRIMMAGE_SERVICE=1, its token differs, or its SDK speaks another protocol "
                "version",
            )
        return self.control_reached(probed, deadline)

    def control_reached(
        self, probed: service_client.Probed, deadline: float
    ) -> service_client.Probed:
        """Ask again while the service's ping fails the way a cluster still setting up its
        routes explains. Only the probe is sent again: no run and no setup request."""
        if not service_client.still_routing(probed.report):
            return probed
        control = cast(dict[str, object], (probed.report or {}).get("control") or {})
        began = deadline - READY_S  # the first probe
        yet = service_client.NOT_YET[str(control.get("error"))]
        print(
            f"scrimmage: waiting for the service to reach the control server at "
            f"{control.get('url')} ({yet})",
            file=sys.stderr,
            flush=True,
        )
        try:
            return self.ask_again(probed, deadline)
        finally:
            self.asked_s = time.monotonic() - began

    def ask_again(self, probed: service_client.Probed, deadline: float) -> service_client.Probed:
        while time.monotonic() < deadline:
            if self.cancelled or self.stop_flag:  # Ctrl-C or SIGTERM ends the wait
                raise Interrupted(self.stop_flag[0] if self.stop_flag else "SIGINT")
            time.sleep(min(CONTROL_STEP_S, max(deadline - time.monotonic(), 0.0)))
            again = service_client.probe(self.config.agent, self.token, self.copy_alive())
            if again.report is None:  # the service stopped answering: keep what the ping found
                self.went_silent = True
                break
            probed = again
            if not service_client.still_routing(probed.report):
                break
        return probed

    def tools_problem(self, report: dict[str, object]) -> str | None:
        """Why the tools the service gates are not the ones the file lists, or None."""
        agent = self.config.agent
        theirs = sorted(str(t) for t in cast(list[object], report.get("tools") or []))
        ours = sorted(agent.tool_names())
        if theirs == ours:
            return None
        names = planner.tools_text(agent.tools, agent.renames)
        imports = ",".join(agent.allowed_imports())
        return (
            f"the service gates the tools {theirs or 'none'}, and this file lists "
            f"{ours or 'none'}; "
            f"start it with SCRIMMAGE_TOOLS={names} and SCRIMMAGE_ALLOW_IMPORTS={imports}"
        )

    def check_service(self) -> None:
        """The probe, then the setup request, before any run. Either can stop the test:
        exit 3 when no gated service answers, exit 2 for a mistake in the setup."""
        probed = self.probe_service()
        report = probed.report
        assert report is not None  # probe_service raised without one
        self.control_ping = cast(dict[str, object] | None, report.get("control"))
        service_client.check_version(report)
        problem = self.tools_problem(report)
        if problem is not None:
            raise service_client.Setup(2, problem)
        service_client.check_control(
            report, self.server.runner_id, asked_s=self.asked_s, silent=self.went_silent
        )
        self.setup_request()

    def setup_request(self) -> None:
        """The first test's baseline, sent once as setup-<hex>: its record is kept, its cost
        counts, and no row counts it. It must be claimed, answer 2xx, refuse nothing, call
        the model inside the run, and see no call outside one."""
        agent = self.config.agent
        test = self.config.tests[0]
        row = planner.Row(test, "baseline", None)
        name = f"setup-{secrets.token_hex(6)}"
        with self.lock:
            self.row_of[name] = row
        plan = planner.plan_for(self.config, row, name)
        self.server.open_run(name, "setup", plan.to_json(), service=True)
        header = f"{name}.{protocol.run_mac(self.token, name)}"
        reply = self.send(header, test.question)
        state = self.wait_record(name, time.monotonic())
        lines = self.store.lines(name)
        self.setup_spent = self.conclude_setup(name, row, state, reply)
        if self.stop_flag:  # Ctrl-C or SIGTERM ended the request, not the service
            raise Interrupted(self.stop_flag[0])
        gate = reply.gate_refused
        if gate == "version":
            raise self.version_refusal(name)
        if gate == "clock":
            raise service_client.Setup(3, service_client.CLOCK_OFF)
        if not state.fetched or gate is not None or reply.failed or reply.timed_out:
            why = gate or reply.failed or ("timed out" if reply.timed_out else "never claimed")
            raise service_client.Setup(
                3, f"the setup request {name} was not served by Scrimmage: {why}"
            )
        refused = [x for x in lines if x.get("action") == "refused" and x.get("reason") != "orphan"]
        if refused:
            first = refused[0]
            raise service_client.Setup(
                2,
                f"the setup request refused {first.get('call')}: "
                f"{first.get('mistake') or first.get('error')}",
            )
        # The reply first: a service that rejected the request never reached its agent.
        ok = reply.status is not None and 200 <= reply.status < 300
        if (
            not ok
            or service_client.streamed(reply, agent)
            or service_client.answer_of(reply, agent)[1]
        ):
            shown = redact(reply.body.decode("utf-8", "replace")[:200], self.scrub)
            kind = reply.headers.get("content-type")
            why = service_client.answer_of(reply, agent)[1] if ok else None
            raise service_client.Setup(
                2,
                f"the setup request got {reply.status} ({kind}): {shown}"
                + (f"; {why}" if why else ""),
            )
        outside = outside_entries(lines)
        if not any(x.get("kind") == "model" for x in lines):
            seen = "; a lost_context notice was seen" if outside else ""
            raise service_client.Setup(
                3,
                "the setup request made no model call inside its run: the context is lost on the "
                f"agent's main path, so every run would be an infrastructure error{seen}",
            )
        if outside:  # reads too: each would end every run open beside it
            adds = "; ".join(f'add "{e}" to [agent] background_real if it is safe' for e in outside)
            raise service_client.Setup(
                2, f"while the setup request was open, calls were made outside any request: {adds}"
            )

    def wait_record(self, run: str, replied: float) -> Any:
        """Close the run once its claimer's last batch came, or record_grace_s plus
        record_wait_s after the reply: a missing last batch is an incomplete record. A
        cancelled test ends the wait at once."""
        agent = self.config.agent
        known = self.server.runs[run]
        if known.claimed_by is not None and not self.cancelled:
            left = replied + agent.record_grace_s + agent.record_wait_s - time.monotonic()
            known.ended.wait(max(0.0, left))
        return self.server.close_run(run)

    def real_writes_in(self, row: planner.Row, lines: list[dict[str, object]]) -> dict[str, int]:
        """The calls of an append or replace row that ran for real and could be writes: the
        ones the load warning names, counted by name."""
        if row.fault is None or row.fault.do not in ("append", "replace"):
            return {}
        listed = real_writes(row.test, self.config.agent)
        found: Counter[str] = Counter()
        for line in lines:
            if line.get("action") != "real" and line.get("under") != "real":
                continue
            label, method, path = str(line.get("call")), line.get("method"), line.get("path")
            if any(
                w.names(label, str(method) if method else None, str(path) if path else None)
                for w in listed
            ):
                found[written_name(label, method, path)] += 1
        return dict(found)

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

    def runner_notice(
        self, run: str, row: planner.Row, kind: str, message: str
    ) -> dict[str, object]:
        """A notice the runner writes itself, as from its own process."""
        return {**self.line(run, row, 1, os.getpid()), "notice": kind, "message": message}

    def left_line(self, run: str, row: planner.Row, pid: int, command: str) -> dict[str, object]:
        text = redact(command, self.scrub)[:COMMAND_CAP]
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
                    "message": redact(str(report["message"]), self.scrub),
                    "mistake": redact(str(report["message"]), self.scrub),
                    "process": {
                        "python": redact(str(process["python"]), self.scrub),
                        "command": redact(str(process["command"]), self.scrub)[:COMMAND_CAP],
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
        completed = False
        try:
            release = self.catch_signals()
            if self.config.agent.service:
                self.waiters = ThreadPoolExecutor(WAITERS, thread_name_prefix="scrimmage-record")
                if self.managed is not None:
                    self.managed.start(self.control_url(self.server.url))
                self.check_service()
            with ThreadPoolExecutor(max_workers=self.config.agent.parallel) as pool:
                base = self.in_turns([row for row in rows if row.fault is None], runs)
                results: list[RunResult | None] = []
                if not self.config.agent.tools and base:
                    # One run alone first, so a misspelled label costs one run, not all.
                    results += settled([self.one(base.pop(0))])
                results += settled(pool.map(self.one, base))
                for test in self.config.tests:
                    mine = [r for r in results if r is not None and r.row.test == test]
                    if (
                        test.faults
                        and mine
                        and all(r.outcome in BASELINE_FAILED_OUTCOMES for r in mine)
                    ):
                        self.skipped.append(test.name)
                faulted = [
                    r for r in rows if r.fault is not None and r.test.name not in self.skipped
                ]
                results += settled(pool.map(self.one, self.in_turns(faulted, runs)))
            with self.lock:
                self.ended = True  # from here the watcher notes nothing, so nothing is noted twice
                name = self.stop_flag[0] if self.stop_flag else ""
                self.stop_flag.clear()  # the watcher and the handler may both have noted it
            if name:  # every run has ended, so it is raised where nothing drops it
                raise Interrupted(name, ended(results), self.cut(results))
            completed = True
        finally:
            try:
                self.cancel()
                if self.waiters is not None:
                    self.waiters.shutdown()
                if self.managed is not None:
                    # After every record's wait, which the copy serves, and before the
                    # handlers go back: while it stops, a signal is only noted, so the stop
                    # always reaches its SIGKILL.
                    self.managed.stop()
            finally:
                release()
                self.server.stop()
                shutil.rmtree(self.answers, ignore_errors=True)
        if completed and self.stop_flag:  # it came while the copy stopped
            raise Interrupted(self.stop_flag[0], ended(results), self.cut(results))
        return [r for r in results if r is not None]

    def cut(self, results: "list[RunResult | None]") -> int:
        """How many runs the signal cut: those it stopped, and those it kept from starting."""
        with self.lock:
            return self.turned_away + sum(
                1 for r in results if r is not None and r.outcome == "stopped"
            )

    def catch_signals(self) -> Callable[[], None]:
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
        for known in list(self.server.runs.values()):  # a service run's record wait ends
            if known.service:
                known.ended.set()

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
                    if self.managed is not None:
                        # A run in flight waits on the copy, so the copy stops now, not after.
                        self.managed.stop()
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


def real_error(
    line: dict[str, object],
    model: Sequence[dict[str, object]] = (),
    scrub: Sequence[str] = (),
) -> str | None:
    """How a model call the gate let through failed by itself, if it did. model: the run's
    model lines, which a 400 the gate marked as after a reply fault is checked against.
    scrub: the run's secrets, taken out of the provider's words again after they are decoded."""
    if line.get("action") != "real":
        return None
    error = str(line.get("error") or "")
    if error and not error.startswith(NOT_A_FAILURE):
        return f"{line.get('call')} failed with {error.split(':')[0]}"
    status = int(str(line.get("status") or 0))
    # The provider's answer to a request the agent built from a faulted reply: its result.
    if status == 400 and line.get("after_reply_fault") is True and reply_fault_before(line, model):
        return None
    if status < 400:
        return None
    said = provider_message(line, scrub)
    return f"{line.get('call')} returned {status}" + (f" ({said})" if said else "")


ERROR_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)')
ERROR_SHOWN = 200  # characters of a provider's error message the report line keeps


def provider_message(line: dict[str, object], scrub: Sequence[str] = ()) -> str:
    """The provider's own words for a failed call, from the line's reply preview, which was
    redacted when it was kept. The JSON error's message when there is one, else the text."""
    result = line.get("result")
    preview = cast(dict[str, object], result).get("preview") if isinstance(result, dict) else None
    if not isinstance(preview, str):
        return ""
    text = preview
    doc: object = None
    try:
        doc = json.loads(preview)
    except ValueError:  # the preview is cut, so look for the message inside the cut JSON
        cut = ERROR_MESSAGE.search(preview)
        if cut is not None:
            text = cut.group(1)
            with contextlib.suppress(ValueError):
                text = str(json.loads(f'"{text}"'))
    found: object = None
    if isinstance(doc, dict):
        body = cast(dict[str, object], doc)
        found = body.get("error", body)
    if isinstance(found, dict):
        message = cast(dict[str, object], found).get("message")
        if isinstance(message, str):
            text = message
    elif isinstance(found, str):
        text = found
    # The preview was redacted as JSON text, where a secret can still sit behind an escape
    # (\/ or \u0026). Decoding made it plain, so redact it again as the plain text it is, and
    # show what a terminal would act on as escapes.
    text = render.plain(" ".join(redact_decoded(text, scrub).split()))
    text = "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cf", "Zl", "Zp") else c for c in text
    )
    return text if len(text) <= ERROR_SHOWN else text[: ERROR_SHOWN - 3] + "..."


# What each provider's library says when it is given no key, as a run's log ends with it. The
# OpenAI library names the variable; the Anthropic one does not, so it is named here.
MISSING_KEY = (
    # The library's own sentence: "Missing credentials. ... set the `OPENAI_API_KEY` or ...".
    # AzureOpenAI names AZURE_OPENAI_API_KEY in the same words, and a log line may only mention
    # the variable, so the name must stand alone and follow the sentence.
    (re.compile(r"Missing credentials\.[^\n]*(?<![A-Z_])OPENAI_API_KEY"), "OPENAI_API_KEY"),
    (re.compile(r"Could not resolve authentication method"), "ANTHROPIC_API_KEY"),
)
LOG_TAIL = 16 * 1024


def missing_key(folder: Path, runs: Sequence[RunResult]) -> str | None:
    """The environment variable a provider's library asked for, when every one of these runs
    ended in its error for a missing key; None when any did not, or no log says it."""
    found: set[str] = set()
    for r in runs:
        try:
            with open(folder / f"{r.run}.log", "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - LOG_TAIL))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            return None
        names = {name for pattern, name in MISSING_KEY if pattern.search(tail)}
        if len(names) != 1:
            return None
        found |= names
    return found.pop() if len(found) == 1 else None


def reply_fault_before(line: dict[str, object], model: Sequence[dict[str, object]]) -> bool:
    """Whether a fault in a model's reply fired in the same process before this line. The
    gate's mark alone is not trusted."""
    proc, seq = proc_key(line.get("proc")), int(str(line.get("seq") or 0))
    return any(
        x.get("action") == "fault"
        and cast(dict[str, object], x.get("fault") or {}).get("do") in REPLY_FAULTS
        and proc_key(x.get("proc")) == proc
        and int(str(x.get("seq") or 0)) < seq
        for x in model
    )


def ended(results: "Iterable[RunResult | None]") -> list[RunResult]:
    """The runs that ended on their own: a run a signal stopped says nothing about the agent."""
    return [r for r in results if r is not None and r.outcome != "stopped"]


class Interrupted(Exception):
    """scrimmage test got SIGTERM or SIGINT.

    results: the runs that ended by themselves before it, not those it stopped.
    cut: how many runs it cut, stopped or kept from starting. 0 means every run had ended.
    """

    def __init__(self, name: str, results: "list[RunResult] | None" = None, cut: int = 0) -> None:
        super().__init__(name)
        self.results = results or []
        self.cut = cut


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
        if not mine or all(r.outcome in BASELINE_FAILED_OUTCOMES for r in mine):
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


def mock_type_lines(config: Config, results: list[RunResult]) -> tuple[list[str], list[str]]:
    """The mock type check, as (warnings, notes): one line per test and mock, however many
    runs and calls said it. A warning counts the runs that said it."""
    warnings: list[str] = []
    notes: list[str] = []
    for test in config.tests:
        mine = [r for r in results if r.row.test == test and r.outcome != "stopped"]
        said: dict[tuple[int, bool], tuple[str, set[str]]] = {}
        for r in mine:
            for line in r.notices:
                index = line.get("mock")
                # A line from another SDK version may lack its mock: it is left out.
                if line.get("notice") == "mock_type" and type(index) is int:
                    key = (index, line.get("note") is True)
                    said.setdefault(key, (str(line.get("message")), set()))[1].add(r.run)
        for (_, note), (message, runs) in sorted(said.items()):
            if note:
                notes.append(f"in test {test.name!r} {message}")
            else:
                warnings.append(
                    f"in test {test.name!r} {message} ({len(runs)} of {len(mine)} runs)"
                )
    return warnings, notes


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


def on_port(config: Config, port: int) -> Config:
    """With start: the config whose address is the copy's on port, so every request goes
    there. Its written form keeps {port}."""
    agent = config.agent
    address = Expanded(service_url(agent, port), str(as_written(agent.service)))
    return dataclasses.replace(config, agent=dataclasses.replace(agent, service=address))


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
    # SIGKILL is not instant: a process killed is still in the group until the kernel has
    # torn it down, so the caller waits for that, a little, before it removes what they used.
    deadline = time.monotonic() + KILLED_S
    while waiting and time.monotonic() < deadline:
        waiting = [proc for proc in waiting if group_alive(proc)]
        if waiting:
            time.sleep(GROUP_POLL_S)
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
    """One row's runs and its result, which the table, the notes and the exit code all read.

    baseline_failed marks a fault row whose test's baseline did not pass. The table and the
    JSON then show `baseline failed` and no rate; the exit code still reads result, so it is
    the same as without the mark.
    """

    row: planner.Row
    runs: list[RunResult]
    result: judge.RowResult
    baseline_failed: bool = False

    @property
    def where(self) -> str:
        return f"{self.row.test.name} {self.row.name}"

    @property
    def shown(self) -> str:
        """The verdict the table and the JSON give."""
        return judge.BASELINE_FAILED if self.baseline_failed else self.result.verdict

    @property
    def refused(self) -> bool:
        return any(r.outcome == "refused call" for r in self.runs)


def row_reports(
    config: Config, results: list[RunResult], interrupted: bool = False
) -> list[RowReport]:
    """interrupted: a signal cut the test, so a baseline with no result was cut, not failed."""
    reports: list[RowReport] = []
    for row in planner.rows(config):
        mine = [r for r in results if r.row == row]
        refused = any(r.outcome == "refused call" for r in mine)
        result = judge.judge_row(
            [r.judgement for r in mine], config.agent.runs, row.rules.pass_rate, refused
        )
        # The baseline is its test's first row, so it is judged before its fault rows.
        baseline = next(r for r in reports if r.row.test == row.test) if row.fault else None
        failed = baseline is not None and baseline.result.verdict != judge.PASS
        if failed and interrupted and baseline is not None:
            failed = baseline.result.verdict != judge.NO_RESULT  # that one was cut, not failed
        reports.append(RowReport(row, mine, result, failed))
    return reports


def used_mocks(lines: list[dict[str, object]]) -> set[int]:
    """The mocks a run used, by place. A mock under a carried fault was used too: its line is a
    fault's, and has the mock key all the same."""
    return {int(str(line["mock"])) for line in lines if "mock" in line}


def written_name(label: str, method: object, path: object) -> str:
    """A call as a note names it: a tool by its name, a web request by method and path."""
    if label.startswith("http:"):
        return f"{method} {label.removeprefix('http:')}{path}"
    return label.removeprefix("tool:")


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def why_unchanged(runs: list[RunResult]) -> str:
    """The reasons calls were left as they were, counted. Empty when none were."""
    reasons = Counter(why for r in runs for why in r.unchanged)
    total = sum(reasons.values())
    return "; ".join(f"{why} ({n} of {total} calls)" for why, n in reasons.most_common())


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
    word = "fail" if report.shown == judge.FAIL else "note"
    head = f"{word}: {report.where}: {row.passed}/{row.judged} passed, pass_rate is {rate:g}"
    calls = [b.text for r in report.runs for b in r.judgement.broken if b.kind == judge.REFUSED]
    if calls:
        call = calls[0].removeprefix(f"{judge.REFUSED} ")
        advice = "See the line below" if call == PROCESS_LABEL else "Mocking it would hide this"
        # Beside a failed baseline the row is not read as a fail, though the call was refused.
        beside = "; its baseline did not pass" if report.baseline_failed else ""
        head = (
            f"{word}: {report.where}: under this fault the agent called {call}, which the test "
            f"does not allow{beside}. {advice}"
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
class ControlFacts:
    """What the runner itself counted of a service test's control channel."""

    runs_sent: int
    claims_received: int
    cut: int
    tls_failed: dict[str, int]
    ping: dict[str, object] | None  # the service's ping to the control server, as it reported


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
    control: ControlFacts | None = None  # a service test only

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
    cut = runner is not None and runner.stopped_by == STOPPED_BY_SIGNAL
    reports = row_reports(config, results, cut)
    table = [HEAD, *(row_cells(row_json(entry, config.agent)) for entry in reports)]
    mistakes: list[str] = []
    infra_lines: list[str] = []  # what failed by itself, as exit 3 says: not the test's mistakes
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
            # A refusal ends a row early. The header counts the runs that ran, so this does too.
            planned = config.agent.runs
            ran_n = min(len(mine), planned)
            cut = f"; {planned} were planned" if ran_n < planned else ""
            notes.append(f"{where}: {verdict.judged} of {ran_n} runs judged{cut}")
        ran = [r for r in mine if r.outcome != "stopped"]
        if row.fault is not None and ran and not any(r.fault_fired for r in ran):
            reasons = why_unchanged(ran)
            mistakes.append(
                f"{where}: the fault never fired in any run, so the row tested nothing"
                + (f". The calls it could not change: {reasons}" if reasons else "")
            )
            mixed = [m.call for m in row.test.mocks if labels.kind_of(m.call) == "tool"]
            if labels.kind_of(row.fault.call) in ("http", "mcp") and mixed:
                mistakes.append(
                    f"{where}: the mock on {', '.join(mixed)} replaces the whole tool, so if "
                    f"that tool makes this call, {row.fault.call} can never fire; "
                    "mock or fault one level, not both"
                )
            if row.fault.model is not None and (why := unused_model(row.fault, ran)):
                mistakes.append(f"{where}: {why}")
        lost = sum(r.undelivered for r in mine)
        if lost:
            notes.append(
                f"{where}: in {lost} of {len(mine)} runs the injected text never reached a model "
                "request; the agent may cut or rewrite tool results (heuristic)"
            )
        written: Counter[str] = Counter()
        for r in mine:
            written.update(r.writes)
        if written:
            names = ", ".join(f"{name} {times(n)}" for name, n in sorted(written.items()))
            count = sum(written.values())
            notes.append(
                f"{where}: {count} real write{'s' if count != 1 else ''} ran ({names}); "
                "see the record"
            )
        notes += [
            f"{where}: {r.run}: {line['message']}"
            for r in mine
            for line in r.notices
            if line.get("notice") in ("left_group", "plan_changed")
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
            if r.outcome == INFRA_ERROR:
                code = INFRA
    stopped_by = runner.stopped_by if runner is not None else None
    unused, wrong, recorded = (
        unused_mocks(config, results) if judged and stopped_by is None else ([], [], [])
    )
    mistakes += wrong
    notes += recorded
    # The tool labels were checked against the tools list, or against the tools a run reported.
    # Only then is an unused tool mock a note rather than a possible typo.
    labels_checked = bool(config.agent.tools) or (
        runner is not None and runner.tools_seen is not None
    )
    if stopped_by is None and labels_checked:
        notes += unused
    if runner is not None and not config.agent.tools:
        mistakes += runner.label_problems
        if (
            stopped_by is None
            and runner.tools_seen is None
            and results
            and any(map(tool_labels, config.tests))
        ):
            mistakes.append(
                "no run reported the agent's tools, so the tool labels in this file were not "
                "checked; see the run logs"
            )
    skipped = runner.skipped if runner is not None else []
    for name in skipped:
        # A baseline that only the provider's failures stopped is not a mistake in the test.
        mine = [r for r in results if r.row.test.name == name and r.row.fault is None]
        said = (
            f"{name}: baseline failed: every baseline run crashed, timed out, was refused, "
            "reached the budget or hit an infrastructure error, so its fault rows did not run"
        )
        if mine and all(r.outcome == INFRA_ERROR for r in mine):
            infra_lines.append(said)
        else:
            mistakes.append(said)
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
            elif (variable := missing_key(folder, ran)) is not None:
                mistakes.append(
                    f"{test.name}: every baseline run stopped before its first model call "
                    f"because {variable} is not set in the environment of the test; set it "
                    "and run again"
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
            infra_lines.append(f"{r.row.test.name} {r.row.name}: {r.run}: {error}, not injected")
    hosted = sum(r.hosted_reads for r in results)
    grouped = miss_groups(results)
    web_misses = sorted(
        {
            f"{doc['call']} {doc['method']} {doc['path']}"
            for doc, _ in grouped
            if doc["reason"] == "request" and doc["action"] == "real"
        }
    )
    # The setup request is no row's run, but its model call was real spend, and the budget
    # held it: the total counts it, so the total is what the provider bills.
    spent = sum(r.cost_usd for r in results) + (runner.setup_spent if runner is not None else 0.0)
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
    # A model's path run as a web request: once per test, however many runs said it.
    notes += sorted(
        {
            f"{r.row.test.name}: {line.get('message')}"
            for r in results
            for line in r.notices
            if line.get("notice") == "model_path"
        }
    )
    typed_warnings, typed_notes = mock_type_lines(config, results)
    warnings += typed_warnings
    notes += typed_notes
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
        messages += [(INFRA_ERRORS, text) for text in dict.fromkeys(infra_lines)]
        messages.append((INFRA_ERRORS, INFRA_LINE))
    elif mistakes:
        code = MISTAKE
    elif judged and any(r.result.verdict == judge.FAIL for r in reports):
        code = ROW_FAILED
    # The team's own statement that writes may run, so it is said first, not under the table.
    above = [(WARNINGS, TOOL_STAR_LINE)] if "tool:*" in config.agent.real else []
    # An injected text may ask for a write, and a write that real allows would then run.
    above += [(WARNINGS, text) for text in write_warnings(config)]
    above += [(WARNINGS, text) for text in carried_model_warnings(config)]
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
        control_facts(runner) if config.agent.service else None,
    )


def control_facts(runner: "Runner | None") -> ControlFacts | None:
    if runner is None:
        return None
    stats = runner.server.stats
    return ControlFacts(
        runner.sent,
        stats.claims_received,
        stats.connections_cut,
        stats.tls_failed,
        runner.control_ping,
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
    for entry in found.rows:  # the cap of 8 filling, before runs fail on it
        busy, p99 = control_load(entry.runs)
        if busy:
            shown = "unknown" if p99 is None else f"{p99:g}"
            print(f"{entry.where}: control: {busy} busy, p99 {shown} ms", file=out)
    for line in control_lines(found.control):
        print(line, file=out)
    budget = config.agent.max_cost_usd
    limit = "no budget" if budget is None else f"a budget of ${budget:g}"
    print(f"model calls cost ${found.spent:.4f} of {limit}", file=out)
    for kind, text in found.messages:
        print(f"{PREFIXES[kind]}: {text}", file=out)
    return found.code


def control_lines(facts: ControlFacts | None) -> list[str]:
    """The runner's own view of a service test's control channel. Runs sent beside claims
    received is what shows a control server that no service can reach, when the figures that
    ride in a claimed run's last batch are missing."""
    if facts is None:
        return []
    lines = [
        f"control server: runs sent {facts.runs_sent}, claims received {facts.claims_received}"
    ]
    if facts.tls_failed:
        reasons = ", ".join(f"{reason} {n}" for reason, n in facts.tls_failed.items())
        lines.append(
            f"control server: {plural(sum(facts.tls_failed.values()), 'connection')} "
            f"failed TLS ({reasons})"
        )
    if facts.cut:
        lines.append(
            f"control server: {plural(facts.cut, 'connection')} cut by the timeouts, "
            "a client that stalled or dripped"
        )
    return lines


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
        **control_json(found.control),
    }


def control_json(facts: ControlFacts | None) -> dict[str, object]:
    if facts is None:
        return {}
    return {
        "runs_sent": facts.runs_sent,
        "claims_received": facts.claims_received,
        "control_cut": facts.cut,
        "control_tls_failed": facts.tls_failed,
        "control_ping": facts.ping,
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
        "text": fault.text,
        "find": None if fault.find is None else redact(fault.find),
        "retry_after": fault.retry_after,
        "form": fault.form,
        "tool": None if fault.tool is None else redact(fault.tool),
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
    if result.judged and not entry.baseline_failed:
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
        "result": entry.shown,
        "broken": [
            {"kind": rule.kind, "text": rule.text, "runs": [r.run for r in runs]}
            for rule, runs in broken_runs(entry)
        ],
        "mock_miss_count": sum(r.miss_count for r in mine),
        "run_details": [run_json(r) for r in mine],
        "test_sha256": row_hash(row.test, fault, agent),
        "test_sha256_v": HASH_VERSION,
        "control_busy": control_load(mine)[0],
        "control_p99_ms": control_load(mine)[1],
    }


def run_json(result: RunResult) -> dict[str, object]:
    answer = None if result.answer is None else redact_decoded(result.answer)[:PREVIEW]
    return {
        "run": result.run,
        "outcome": result.outcome,
        "fault_fired": result.fault_fired,
        "judged": result.judgement.verdict,
        "left_out": result.judgement.left_out,
        "cost_usd": round(result.cost_usd, 8),
        "answer_preview": answer,
        "mock_miss_count": result.miss_count,
        "control_busy": result.control_busy,
        "control_p99_ms": result.control_p99_ms,
    }


def control_load(runs: list[RunResult]) -> tuple[int | None, float | None]:
    """A row's control load: the busy counts summed and the largest p99, of the runs that
    reported one. Each describes the process that served it, not the run alone."""
    busy = [r.control_busy for r in runs if r.control_busy is not None]
    p99 = [r.control_p99_ms for r in runs if r.control_p99_ms is not None]
    return (sum(busy) if busy else None), (max(p99) if p99 else None)


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
