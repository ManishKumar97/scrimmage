"""Run state: a run's plan, counters, record and the refused-run poison, and what the
process knows apart from any run.

A function or a script is one run per process, so the plan lives in one slot shared by
the whole process. A child process has no plan until its first gated call, so a short
command that makes no calls costs nothing.

A service is one process that serves many runs, and plain requests, at once. There each
request's run is in a context variable, SLOT, which the entry gate sets on every request;
what the process learns, such as the tools a framework made at startup, is in its
ProcessState, which every run reads.
"""

import dataclasses
import hashlib
import json
import math
import os
import shlex
import socket
import sys
import threading
import time
import weakref
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol, TypeVar, cast

from scrimmage_core import capture, labels
from scrimmage_core import decide as engine
from scrimmage_core import provider_openai as openai_table
from scrimmage_core.changes import Changed
from scrimmage_core.decide import missed
from scrimmage_core.errors import ScrimmageRefused, ScrimmageStopped, ScrimmageTimeLimit
from scrimmage_core.outcomes import PREVIEW
from scrimmage_core.plan import Plan
from scrimmage_core.records import RECORD_VERSION
from scrimmage_core.redact import redact, redact_decoded, redacted, remove_values, scrubbed

BLOB_CAP = 10 * 1024 * 1024  # a side-folder item keeps its first 10 MB and is marked cut
BLOB_WAIT_S = 10.0  # the longest a recording call waits for room for its saved value
BLOB_POLL_S = 0.005
CLOSED = "a run that has ended"
PLAN_RETRY_S = 1.0  # a failed plan fetch can take ten seconds, so not one per line
COMMAND_CAP = 1000  # a process's line keeps this much of its command, which can hold a question
# The process and call_id of the real tool call whose code is running, in a recording run.
# A context variable follows await, the tasks made inside the tool and asyncio.to_thread,
# which copy it, but not a thread or a pool the tool starts itself. A forked child inherits
# it, so it holds the process too: a call in another process names no parent.
PARENT: ContextVar[tuple[int, int] | None] = ContextVar("scrimmage_parent", default=None)
# Every context variable Scrimmage defines. A thread or a pool task a run starts carries
# these, and only these, so the team's own variables are never changed by Scrimmage.
CARRIED: list[ContextVar[Any]] = [PARENT]
V = TypeVar("V")


def carried(var: ContextVar[V]) -> ContextVar[V]:
    """Register one of Scrimmage's context variables, so a run's threads carry it."""
    CARRIED.append(var)
    return var


class Marker:
    """A value of SLOT that is not a run: a plain request, or code outside any request."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


class Plain(Marker):
    """A request with no run, one per request. Once its entry call has returned, a thread or
    task it started that is still at work is background work: done, and read as outside."""

    def __init__(self) -> None:
        super().__init__("PLAIN")
        self.done = False


OUTSIDE = Marker("OUTSIDE")  # outside any request: startup, lifespan, a pool between tasks


def is_plain(slot: object) -> bool:
    """Code of a plain request still being served."""
    return isinstance(slot, Plain) and not slot.done


class Joined(NamedTuple):
    """Code that belongs to a run, and that run's state."""

    run: str
    state: "RunState"


# The run this code belongs to, in a service. Unset for a function or a script, which keep
# their one run in the process slot below.
SLOT: ContextVar["Joined | Marker | None"] = carried(ContextVar("scrimmage_slot", default=None))
# A request a client in this process sends to an app in this process, such as a test
# client's: the app's entry gate keeps the caller's slot.
IN_PROCESS_CALL: ContextVar[bool] = carried(ContextVar("scrimmage_in_process", default=False))


class LineSink(Protocol):
    def put(self, line: dict[str, object], *, keep: bool = False) -> None: ...


class BlobSink(Protocol):
    """The sender: side-folder items and settlements it delivers off the agent's thread."""

    def put_blob(self, sha: str, data: bytes) -> bool: ...

    def put_settle(self, reservation: int, usd: float) -> None: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def content(value: object, *, clean: bool = True, scrub: Sequence[str] = ()) -> dict[str, object]:
    """Hash, size and preview of a recorded value, with secrets removed first.

    The call is only observed, so no value may fail it: one that holds itself, is nested
    deeper than the stack, holds a lone surrogate or has a repr that fails is still
    recorded, as far as it can be. clean=False keeps the text as it is: the injected text of
    a fault, which the team wrote and the line must show as written.
    """
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=shown, sort_keys=True)
    except Exception as exc:  # a value that holds itself, nests too deep, or will not sort
        text = f"<a {type(value).__name__} Scrimmage could not write as JSON: {type(exc).__name__}>"
    if clean:  # a Python string has no escapes left in it; JSON text is read as JSON
        text = redacted(text, decoded=isinstance(value, str), scrub=scrub)[0]
    # A lone surrogate, such as an undecodable byte in a file name, has no UTF-8 form.
    text = text.encode("utf-8", "backslashreplace").decode()
    data = text.encode()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "preview": text[:PREVIEW],
    }


def shown(value: object) -> str:
    """An item JSON cannot hold, as its repr, or the plain object form if its repr fails."""
    try:
        return repr(value)
    except Exception:
        return object.__repr__(value)


@dataclass
class Step:
    """One gated call between its decision and its end."""

    call: engine.Call
    decision: engine.Decision
    args: dict[str, object]
    start: str
    started: float
    extra: dict[str, object] = field(default_factory=dict[str, object])
    call_id: int | None = None  # in a recording run: this call's number in its process
    parent: int | None = None  # the real tool call this one was made inside, if any
    miss: engine.Miss | None = None  # the call had a mock on its label and no mock named it
    # A carried fault (append, replace, slow), as the line says it: what changed, why nothing
    # did, how long the call waited and what ended the wait early.
    changed: dict[str, object] | None = None
    unchanged: dict[str, object] | None = None
    slowed_ms: int | None = None
    cut_by: str | None = None
    waiting_since: float | None = None  # while a slowed call waits: when it began


def this_process() -> dict[str, object]:
    return {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso()}


def model_path_hint(label: str, detail: str) -> str | None:
    """For a POST to a model's path on a host no one named: where to name it. None for any
    other request. Listed under real it would run as a plain web request, never budgeted."""
    method, _, path = detail.partition(" ")
    if not label.startswith("http:") or openai_table.op_for(path, method) is None:
        return None
    host = label.removeprefix("http:")
    return (
        f"{label} {detail} is a model request to a host not under [agent] model_hosts, so it "
        f'was refused as a web write. Add {{host = "{host}", provider = "openai"}} to [agent] '
        "model_hosts; a model call listed under real is never budgeted or faulted by model name."
    )


def newer_version_message(
    library: str, found: tuple[int, int] | None, tested: tuple[tuple[int, int], tuple[int, int]]
) -> str | None:
    """The note for a library newer than the versions this release was tested with."""
    oldest, newest = tested
    if found is None or found <= newest:
        return None
    return (
        f"{library} {found[0]}.{found[1]} is newer than the versions this release was "
        f"tested with ({oldest[0]}.{oldest[1]} to {newest[0]}.{newest[1]})"
    )


def outlived_message(name: str) -> str:
    return f"{name} started in this run and outlived it; start it in lifespan"


def stop_process(message: str) -> None:
    """End this process at once: a gate cannot work, so nothing may run without it."""
    print(f"scrimmage: {message}; the program stops", file=sys.stderr)
    sys.stderr.flush()
    os._exit(1)


class ProcessState:
    """What this process knows, whatever run it serves: the names its gates reach, what it
    noted at startup, and whether its gates still work.

    A function or a script has one of these and one run. A service has one and many runs,
    so a tool a framework made at startup, before any run, is seen by every later run.
    """

    def __init__(self, tools: Iterable[str] = (), *, service: bool = False) -> None:
        self.listed = set(tools)  # names gates will reach
        self.adapted: set[str] = set()  # names a framework's adapter has checked
        self.blocked: dict[str, str] = {}  # names a call could skip a gate by, and why
        self.rechecks: list[Callable[[], None]] = []  # an adapter's check of its tools
        self.service = service
        # Notices made with no run, such as a library version at import: the startup report.
        self.report: list[dict[str, object]] = []
        # A service listens from the entry gate's first call. A gate that breaks after that
        # marks the process broken instead of ending it: run requests are then refused.
        self.listening = False
        self.broken: str | None = None
        self.stop: Callable[[str], None] = stop_process  # ends it, before a service listens
        # A function's or a script's one run. A service has none: its runs come and go.
        self.own: RunState | None = None
        self.lock = threading.Lock()
        # A service's runs whose record is open, by name.
        self.runs: dict[str, RunState] = {}
        # A thread or task that held a run past its end and still made a gated call: its
        # name. The next run this process claims is told, as it cannot be a result.
        self.stale_worker: str | None = None
        self.orphans_told: set[str] = set()  # runs an orphan's refusal was printed for

    def register(self, run: "RunState") -> None:
        with self.lock:
            self.runs[run.name] = run

    def unregister(self, run: "RunState") -> None:
        with self.lock:
            if self.runs.get(run.name) is run:
                del self.runs[run.name]

    def open_runs(self) -> "list[RunState]":
        with self.lock:
            return list(self.runs.values())

    def claimed(self, run: "RunState") -> None:
        """A run was just claimed. A worker that outlived an earlier run means the context of
        this process cannot be trusted, so this run is an infrastructure error, named."""
        with self.lock:
            stale = self.stale_worker
        if stale is not None:
            run.notice("structure", "scrimmage", outlived_message(stale))

    def current(self) -> "RunState | None":
        """The run this code belongs to; in a function or a script, always its one run."""
        run = current()
        return run if run is not None or self.service else self.own

    def notice(self, kind: str, library: str, message: str) -> None:
        """A notice that may come with no run: it goes to the run, if there is one, else to
        the startup report and stderr."""
        run = self.current()
        if run is not None:
            run.notice(kind, library, message)
            return
        with self.lock:
            self.report.append({"notice": kind, "library": library, "message": message})
        about = "" if library == "scrimmage" else f"{library}: "  # never "scrimmage: scrimmage:"
        print(f"scrimmage: {about}{message}", file=sys.stderr)

    def newer_version(
        self,
        library: str,
        found: tuple[int, int] | None,
        tested: tuple[tuple[int, int], tuple[int, int]],
    ) -> None:
        message = newer_version_message(library, found, tested)
        if message is not None:
            self.notice("newer_version", library, message)

    def structure(self, library: str, message: str, stop: Callable[[str], None]) -> None:
        """A library its gate cannot work with. The process stops, so nothing runs ungated;
        in a service that is already listening it is marked broken instead, so every run
        request is refused and plain requests go on untouched."""
        self.notice("structure", library, message)
        if self.service and self.listening:
            with self.lock:
                self.broken = self.broken or message
            return
        stop(message)


class RunState:
    counters: engine.Counters

    def __init__(
        self,
        plan: Plan | None,
        sink: LineSink,
        allowed: list[str],
        *,
        load: Callable[[], Plan] | None = None,
        tools: tuple[str, ...] = (),
        blobs: BlobSink | None = None,
        process: ProcessState | None = None,
    ) -> None:
        """A child passes no plan: load fetches it, and tools names its gates until then.

        process: what the process knows, shared by all its runs; with none, a run of its own.
        """
        self.child = plan is None
        self._plan: Plan | None = None
        self._load = load
        self._missing: Plan | None = None  # why the last fetch failed, kept for a moment
        self._try_again_at = 0.0
        self._plan_lock = threading.Lock()
        self.sink = sink
        self.blobs = blobs  # where a recorded value's content goes
        if process is None:
            process = ProcessState(tools if plan is None else plan.tool_names())
        if not process.service and process.own is None:
            process.own = self
        self.process = process
        self.allowed = allowed
        self.began = time.monotonic()  # until a plan says how long the run has been going
        self.refused: str | None = None
        self.model_calls = 0
        self.proc = this_process()
        self._seq = 0
        self._ids = 0
        self._lock = threading.Lock()
        self._unannounced = self.child  # a child says what it is on its first line
        self.live: dict[int, Step] = {}  # calls decided and not yet finished
        # Set when the run is stopped or has ended while this process lives on, so a call
        # that is only waiting, such as a slowed one, ends at once and does not run.
        self.stopped = threading.Event()
        # The text each changed call put in a result, so a later model request can be
        # looked at for it: a heuristic note when it never got there.
        self.injected: list[str] = []
        self.end_program: Callable[[], None] = lambda: None  # set where a run has a sender
        # The web gate's replies not read to their end, and the model paths it ran as web
        # requests, each noted once: both belong to this run, never to the process.
        self.open: set[Callable[[bool], None]] = set()
        self.model_paths: set[str] = set()
        self.name = "" if plan is None else plan.run
        # In a service: the request's own call into the app has returned, so a later call
        # in this run comes from a thread or task that outlived it.
        self.entry_done = False
        # On Python 3.11, the asyncio tasks made while code belonged to this run; 3.12 and
        # later read each task's context instead.
        self.tasks: weakref.WeakSet[Any] = weakref.WeakSet()
        if plan is not None:
            self._arrived(plan)

    def _arrived(self, plan: Plan) -> Plan:
        """Take a plan the runner just sent. Its elapsed_s is the run's age on the runner's
        clock, so the run's time counts from when the runner started it, not from when this
        process loaded. Only a length of time crosses between the two processes' clocks."""
        self.began = time.monotonic() - plan.elapsed_s
        return self._take(plan)

    def _take(self, plan: Plan) -> Plan:
        # Each process counts its own calls, so "the third call" would mean something
        # different in each. Faults fire only in the program the runner started.
        if self.child and not plan.children:
            plan = dataclasses.replace(plan, faults=())
        self._plan = plan
        self.counters = engine.Counters(plan)
        return plan

    @property
    def plan(self) -> Plan:
        plan = self._plan
        if plan is not None:
            return plan
        with self._plan_lock:
            if self._plan is not None:
                return self._plan
            if self._missing is not None and time.monotonic() < self._try_again_at:
                return self._missing
            assert self._load is not None
            fetched = self._load()
            if fetched.unavailable is None:
                return self._arrived(fetched)
            self._missing = fetched
            self._try_again_at = time.monotonic() + PLAN_RETRY_S
            return fetched

    @property
    def listed(self) -> set[str]:
        return self.process.listed

    @property
    def adapted(self) -> set[str]:
        return self.process.adapted

    @property
    def blocked(self) -> dict[str, str]:
        return self.process.blocked

    @property
    def rechecks(self) -> list[Callable[[], None]]:
        return self.process.rechecks

    @property
    def seen(self) -> set[str]:
        """Names a gate reaches, as the model sees them, as the process knows them: a tool
        learned before any run is seen by every run. A request carries only names, so one
        tool that can skip its gate blocks every tool of that name."""
        return (self.listed | self.adapted) - self.blocked.keys()

    def recheck_tools(self) -> None:
        """Bring the names up to date: a class can change after its tools were made."""
        for recheck in self.rechecks:
            recheck()

    def close_open(self) -> None:
        """A reply the agent never finished reading is still a call that was made."""
        for finish in list(self.open):
            finish(False)

    def check_time(self) -> None:
        """The time limit counts from when the runner started the run, as the plan says.

        The runner stops the program at run_timeout first, so here it is a backstop; it
        matters where one process outlives its runs.
        """
        if time.monotonic() - self.began > self.plan.time_limit_s:
            limit = self.plan.time_limit_s
            raise ScrimmageTimeLimit(f"the run passed its time limit of {limit:g}s")

    def decide(self, call: engine.Call, args: dict[str, object]) -> Step:
        """Count the call and decide what it gets. A poisoned run refuses every call."""
        self.check_time()
        plan = self.plan
        with self._lock:
            if self.refused is not None:
                decision = engine.Decision("refused", reason="poisoned")
            elif plan.unavailable is not None:
                decision = engine.Decision("refused", reason="no_plan")
            else:
                numbers = self.counters.note(call)
                decision = engine.decide(call, plan, numbers)
                if decision.action == "refused":
                    self.refused = call.label
            step = Step(call, decision, args, now_iso(), time.monotonic())
            step.miss = self.found_miss(call, plan, decision)
            return self._going(step)

    @staticmethod
    def found_miss(call: engine.Call, plan: Plan, decision: engine.Decision) -> engine.Miss | None:
        """Whether the call is a miss. This runs on the agent's call, so a failure in it must
        not reach the agent: the decision stands and the miss is marked unreadable."""
        try:
            return missed(call, plan, engine.beneath(decision))
        except Exception:
            return engine.Miss("unreadable")

    def _going(self, step: Step) -> Step:
        """Track a call until it ends. Called with the lock held, so ids follow decisions."""
        if self.recording:
            self._ids += 1
            inside = PARENT.get()
            parent = inside[1] if inside is not None and inside[0] == os.getpid() else None
            step.call_id, step.parent = self._ids, parent
        self.live[id(step)] = step
        return step

    @property
    def recording(self) -> bool:
        """Whether this is a recording run. A child with no plan yet records nothing."""
        plan = self._plan
        return plan is not None and plan.record

    def with_args(self, call: engine.Call, arguments: object) -> engine.Call:
        """The call with its arguments as a recorded mock holds them, when one could match.

        A plan with no mock that has args never pays for the conversion, unless it is
        recording: a recording run needs to know whether the arguments were plain.
        """
        if not self.plan.record and not any(mock.args is not None for mock in self.plan.mocks):
            return call
        return dataclasses.replace(call, args=capture.converted(arguments))

    def begin(self, name: str, args: dict[str, object]) -> Step:
        return self.decide(self.with_args(engine.Call("tool", f"tool:{name}", name), args), args)

    def recorded(self, find: Callable[[], dict[str, object]]) -> dict[str, object]:
        """A recorded field for a line. Recording only observes the call, so no failure in
        it, Scrimmage's own included, may reach the agent: it is listed as unsavable."""
        try:
            return self.save(find())
        except Exception as exc:
            return capture.scrimmage_error(exc)

    def save(self, doc: dict[str, object]) -> dict[str, object]:
        """A recorded field for a line: the value and arguments go to the side folder."""
        out = dict(doc)
        for key in ("value", "args"):
            if key in out:
                text = json.dumps(out[key], ensure_ascii=False)  # plain refused surrogates
                text = remove_values(text, self.secrets)[0]  # the patterns ran already
                out[key] = kept_blob(text.encode(), self.blobs, wait=True)
        return out

    def passed(self, call: engine.Call) -> Step:
        """A call that is recorded but not decided, so it is not counted and cannot be refused."""
        with self._lock:
            return self._going(Step(call, engine.Decision("real"), {}, now_iso(), time.monotonic()))

    def refuse(self, step: Step, reason: str) -> Step:
        """Turn a decided call into a refusal for a reason the gate found, and poison the run."""
        with self._lock:
            self.refused = self.refused or step.call.label
        step.decision = engine.Decision("refused", reason=reason)
        return step

    def refusal(self, step: Step, detail: str = "") -> ScrimmageRefused:
        label = step.call.label
        if step.decision.reason == "no_plan":
            return ScrimmageRefused(
                f"{label}: this process could not get its run's plan, so the call is refused: "
                f"{self.plan.unavailable}"
            )
        if self.refused == CLOSED:
            return ScrimmageRefused(f"{label}: this process's run has ended, so every call fails")
        reason = step.decision.reason or "unlisted"
        if reason == "unlisted" and step.miss is not None and step.miss.reason == "arguments":
            reason = "arguments"
        first = self.refused or label
        if reason == "poisoned" or first != label:
            return ScrimmageRefused(
                f"{label}: this run already refused {first}, so every later call fails"
            )
        messages = {
            "unlisted": (
                f"{label} is neither mocked by this test nor listed under [agent] real. "
                f'Mock it in the test, or add "{label}" to real if it is safe to run for real.'
                f"{detail}"
            ),
            "arguments": (
                f"{label} has mocks in this test, but none matches this call's arguments, and "
                "it is not under [agent] real by name, so it is refused. Add a mock for these "
                f'arguments, or add "{label}" to real if it is safe to run for real.'
            ),
            "web_write": (
                model_path_hint(label, detail)
                or f"{label} {detail} is a web request that can change data, and this test "
                "neither mocks it nor lists it under [agent] real. Mock it in the test, or "
                f'add "{label} {detail}" to real if it is safe to run for real.'
            ),
            "budget": f"{label}: the test's budget cannot cover this call: {detail}",
            "budget_held": f"{label}: the budget could not hold this call in time: {detail}",
            "no_price": detail,
            "hosted": detail,
            "unknown_tool_type": detail,
            "unseen_tool": detail,
            "unreadable_body": detail,
            "unreadable_model": (
                f"{label}: a fault in this test names a model, but {detail} has no model name "
                "Scrimmage can read, so it cannot tell whether the fault is for this call. Send "
                "this call as plain JSON with a model field. Taking model off the fault would "
                "hit every call its label names, and with a budget any model call with no name "
                'needs a price, as "" under [agent.prices]; see docs/model-faults.md'
            ),
            "redirect_off_host": detail,
            "unsupported_loop": detail,
            "ambiguous_label": detail,
            "blind_child": detail,
            "orphan": detail,
            "probe": (
                f"{label} {detail}: scrimmage init --probe stops at the first model request, "
                "which is refused before it is sent"
            ),
        }
        return ScrimmageRefused(messages.get(reason, f"{label} is refused: {detail}"))

    def miss_line(self, step: Step) -> dict[str, object]:
        """A miss as it goes on the line. It runs on the agent's call, so no failure in it may
        reach the agent: the line then says the evidence could not be built."""
        try:
            return miss_evidence(step.call, self.plan, step.miss)
        except Exception:
            return fit_miss(unreadable_miss())

    def run_closed(self) -> None:
        """The run ended and this process lived on, so nothing it does can be recorded."""
        self.refused = self.refused or CLOSED
        self.stopped.set()

    def forked(self) -> None:
        """This process is a copy of its parent, so nothing it holds about the process is true.

        A lock another thread held at the fork would stay locked here for good.
        """
        self._lock = threading.Lock()
        self._plan_lock = threading.Lock()
        self._missing = None
        self.child = True
        self.proc = this_process()
        self._seq = 0
        self._ids = 0
        self.live = {}
        self.open = set()  # replies the parent is reading
        self._unannounced = True
        self.stopped = threading.Event()  # a copy of the parent's event is the parent's
        if self._plan is not None:
            self._take(self._plan)

    def announce(self) -> None:
        """Say once what this process is: its Python, its command and its parent."""
        self._unannounced = False
        command = redact(shlex.join(sys.orig_argv), self.secrets)[:COMMAND_CAP]
        line = self._head()
        line.update(
            notice="process",
            library="scrimmage",
            message="a process joined the run",
            process={"python": redact(sys.executable), "command": command, "parent": os.getppid()},
        )
        self.put(line, keep=True)

    def stop_open(self) -> None:
        """A call going on when the run is stopped would leave no line, so it gets one here."""
        self.stopped.set()
        with self._lock:
            steps, self.live = list(self.live.values()), {}
        for step in steps:
            if step.waiting_since is not None:  # a slowed call that was still waiting
                step.cut_by = "run stopped"
                step.slowed_ms = int((time.monotonic() - step.waiting_since) * 1000)
            self._write(step, error=ScrimmageStopped("the run was stopped during this call"))

    def _head(self) -> dict[str, object]:
        plan = self.plan
        return {
            "v": RECORD_VERSION,
            "run": plan.run,
            "test": plan.test,
            "row": plan.row,
            "seq": self.next_seq(),
            "proc": self.proc,
        }

    def _first_line(self) -> None:
        with self._lock:
            first, self._unannounced = self._unannounced, False
        if first:
            self.announce()

    def cannot_gate(self, name: str, message: str) -> None:
        """A tool no gate can reach: a mistake that ends the run.

        The real function must never run ungated, so the run is poisoned and the line is
        kept even on a full queue.
        """
        step = Step(
            engine.Call("tool", f"tool:{name}", name),
            engine.Decision("refused", reason="unlisted"),
            {},
            now_iso(),
            time.monotonic(),
            extra={"by_config": True},  # the config caused this refusal, not the agent
        )
        with self._lock:
            self.refused = self.refused or step.call.label
            self._going(step)
        self.finish(step, error=ScrimmageRefused(message), mistake=message)

    def outside_refused(self, call: engine.Call, message: str, entry: str) -> None:
        """A write made outside any request while this run was open was refused. The line is
        this run's, so the run is an infrastructure error: it cannot tell the call was not
        its own. The run is not poisoned: its own calls still follow its plan."""
        step = Step(
            call,
            engine.Decision("refused", reason="orphan"),
            {},
            now_iso(),
            time.monotonic(),
            extra={"outside": True, "background_entry": entry},
        )
        with self._lock:
            self._going(step)
        self.finish(step, error=ScrimmageRefused(message))

    def newer_version(
        self,
        library: str,
        found: tuple[int, int] | None,
        tested: tuple[tuple[int, int], tuple[int, int]],
    ) -> None:
        """Note a library newer than the versions this release was tested with."""
        message = newer_version_message(library, found, tested)
        if message is not None:
            self.notice("newer_version", library, message)

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def notice(
        self,
        kind: str,
        library: str,
        message: str,
        *,
        mistake: str | None = None,
        tools: list[str] | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        """A line that is not a call, such as a library version the gate cannot use."""
        self._first_line()
        line = self._head()
        line.update(notice=kind, library=library, message=message)
        if mistake is not None:
            line["mistake"] = redact(mistake, self.secrets)
        if tools is not None:
            line["tools"] = tools
        if extra is not None:
            line.update(extra)
        self.put(line, keep=True)

    def unchanged(self, step: Step, why: str) -> None:
        """A carried fault could not change this call: it gets what the baseline gives it, and
        the line says why. The call is the baseline's, so it is not a fired fault."""
        decision = step.decision
        fault = None if decision.fault is None else self.plan.faults[decision.fault]
        if decision.under is not None:
            step.decision = engine.Decision(
                decision.under, mock=decision.mock, under=decision.under
            )
        step.unchanged = {"do": "" if fault is None else fault.do, "why": why}

    def changed(self, step: Step, done: Changed) -> None:
        """Say what a carried fault changed. The text is the fault's own, shown as written;
        find is cleaned for its preview and hashed as written."""
        assert step.decision.fault is not None
        fault = self.plan.faults[step.decision.fault]
        shown: dict[str, object] = {
            "text": content(remove_values(fault.text or "", self.secrets)[0], clean=False),
            "count": done.count,
            "parts": list(done.parts),
        }
        if fault.find is not None:
            raw = fault.find.encode("utf-8", "backslashreplace")
            shown["find"] = {
                **content(fault.find, scrub=self.secrets),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        step.changed = shown
        # A changed model reply is read by the agent, not sent on, so it is not looked for.
        if fault.text and fault.text not in self.injected and step.call.kind != "model":
            self.injected.append(fault.text)

    def finish(
        self,
        step: Step,
        *,
        result: object = None,
        error: BaseException | None = None,
        mistake: str | None = None,
    ) -> None:
        """Write a call's line, unless the run was stopped and it already has one."""
        with self._lock:
            going = self.live.pop(id(step), None) is not None
        if going:
            self._write(step, result=result, error=error, mistake=mistake)
        # A refusal is an Exception the agent can catch, and its next plain tool would then
        # run. In a probe nothing is needed past this point, so no more of its code runs.
        if step.decision.action == "refused" and self._plan is not None and self._plan.probe:
            self.end_program()

    def _write(
        self,
        step: Step,
        *,
        result: object = None,
        error: BaseException | None = None,
        mistake: str | None = None,
    ) -> None:
        decision = step.decision
        secrets = self.secrets
        self._first_line()
        line = self._head()
        refused = error is not None or decision.action == "refused"
        line.update(
            call=redact(step.call.label, secrets),  # a server names its own tools
            kind=step.call.kind,
            action=decision.action,
            args=content(step.args, scrub=secrets),
            result=None if refused else content(result, scrub=secrets),
            error=None if error is None else error_text(step.call, error, secrets),
            start=step.start,
            ms=int((time.monotonic() - step.started) * 1000),
        )
        if decision.fault is not None:
            fault = self.plan.faults[decision.fault]
            line["fault"] = {"do": fault.do, "when": fault.when, "n": decision.n}
        if decision.mock is not None:
            line["mock"] = decision.mock
        if decision.under is not None:
            line["under"] = decision.under
        if step.changed is not None:
            line["changed"] = step.changed
        if step.unchanged is not None:
            line["unchanged"] = step.unchanged
        if step.slowed_ms is not None:
            line["slowed_ms"] = step.slowed_ms
        if step.cut_by is not None:
            line["cut_by"] = step.cut_by
        if decision.action == "refused":
            line["reason"] = decision.reason or "unlisted"
        if mistake is not None:
            line["mistake"] = redact(mistake, secrets)
        if step.call_id is not None:
            line["call_id"] = step.call_id
        if step.parent is not None:
            line["parent"] = step.parent
        line.update({key: scrub(value, secrets) for key, value in step.extra.items()})
        if step.miss is not None:
            line["mock_miss"] = self.miss_line(step)
        # On every recorded tool or MCP line, refused ones too: a refused call is the one a
        # skeleton is written for, and its preview can parse while no mock could match it.
        if self.recording and step.call.kind in ("tool", "mcp") and step.call.args is None:
            line["args_not_plain"] = True  # the preview holds a repr, which no mock can match
        write = step.call.kind == "http" and step.call.method not in engine.READ_METHODS
        # Nothing tells an MCP read from an MCP write, so a real one is kept as a write is,
        # also when a carried fault changed or delayed it.
        ran = decision.action == "real" or decision.under == "real"
        write = write or (step.call.kind == "mcp" and ran)
        write = write or step.call.kind == "process"
        keep = decision.action == "refused" or step.call.kind == "model" or write
        keep = keep or "mock_miss" in line
        self.put(line, keep=keep)

    @property
    def secrets(self) -> tuple[str, ...]:
        """The values this run's plan says to remove from every line, as the runner read them
        from the environment. This run's own: another run's are never used here."""
        plan = self._plan
        return () if plan is None else plan.scrub

    def put(self, line: dict[str, object], *, keep: bool) -> None:
        """Every text a line holds goes without this run's values, whatever field holds it."""
        self.sink.put(cast("dict[str, object]", scrubbed(line, self.secrets)), keep=keep)


MISS_CAP = 2048  # a miss on a line, as JSON text; a batch over 4 MB is refused with a 413
DIFFERS_CAP, KEY_CUT = 20, 64
FIRST_KEYS = 5


def unreadable_miss() -> dict[str, object]:
    return {
        "reason": "unreadable",
        "seen": None,
        "method": None,
        "path": None,
        "nearest": None,
    }


def miss_evidence(call: engine.Call, plan: Plan, miss: engine.Miss | None) -> dict[str, object]:
    """The miss as display text, cleaned by the heuristic in capture.shown_args, and capped."""
    if miss is None or miss.reason == "unreadable":
        return fit_miss(unreadable_miss())
    differs = list(miss.differs)
    nearest: dict[str, object] | None = None
    if miss.nearest is not None:
        mock = plan.mocks[miss.nearest]
        nearest = {
            "mock": miss.nearest,
            "args": None if mock.args is None else capture.shown_args(mock.args, differs),
            "method": mock.method,
            "path": None if mock.path is None else redact(mock.path)[:PATH_CUT],
            "differs": [
                redact(capture.cleaned_text(k, decoded=True))[:KEY_CUT]
                for k in differs[:DIFFERS_CAP]
            ],
            "differs_left_out": max(len(differs) - DIFFERS_CAP, 0),
        }
    web = miss.reason == "request"
    seen = None
    if miss.reason == "no_mock":
        seen = capture.shown_args(call.args, ())
    elif miss.reason == "arguments" and miss.known:
        seen = capture.shown_args(call.args, differs)
    return fit_miss(
        {
            "reason": miss.reason,
            "seen": seen,
            "method": call.method if web else None,
            "path": redact(call.name)[:PATH_CUT] if web else None,
            "nearest": nearest,
        }
    )


PATH_CUT = 200


def fit_miss(miss: dict[str, object]) -> dict[str, object]:
    """Keep a miss under MISS_CAP as JSON text, giving up the most detailed parts first."""

    def size() -> int:
        return len(json.dumps(miss))

    nearest = cast("dict[str, object] | None", miss["nearest"])
    if size() > MISS_CAP:
        miss["seen"] = None
        if nearest is not None:
            nearest["args"] = None
            keys = cast(list[str], nearest["differs"])
            nearest["differs_left_out"] = int(str(nearest["differs_left_out"])) + max(
                len(keys) - FIRST_KEYS, 0
            )
            nearest["differs"] = keys[:FIRST_KEYS]
    if size() > MISS_CAP:
        miss["path"] = None
        if nearest is not None:
            nearest["path"] = None
            nearest["differs_left_out"] = int(str(nearest["differs_left_out"])) + len(
                cast(list[str], nearest["differs"])
            )
            nearest["differs"] = []
    return miss


def error_text(call: engine.Call, error: BaseException, secrets: Sequence[str] = ()) -> str:
    """A web client's message can quote the URL or a header, so a web line keeps its type."""
    if call.kind == "http" and not isinstance(error, ScrimmageRefused):
        return type(error).__name__
    try:
        message = str(error)
    except Exception:  # an error whose own __str__ fails must not fail the call it observed
        message = "its message could not be read"
    text = redact(f"{type(error).__name__}: {message}", secrets)
    return text.encode("utf-8", "backslashreplace").decode()


def scrub(value: object, secrets: Sequence[str] = ()) -> object:
    """Remove secrets from the text a gate puts on a line, such as a model name or a path."""
    if isinstance(value, str):  # a value the gate read, such as a model name out of the body
        return redact_decoded(value, secrets)
    if isinstance(value, list):
        return [scrub(item, secrets) for item in cast(list[object], value)]
    return value


def handed_over(sink: BlobSink, sha: str, data: bytes, *, wait: bool) -> bool:
    """Queue one side-folder item. A recording run must lose none, so it waits for room, up
    to a bound: a sender that never drains must not hang the agent's call."""
    if sink.put_blob(sha, data):
        return True
    deadline = time.monotonic() + BLOB_WAIT_S if wait else 0.0
    while time.monotonic() < deadline:
        time.sleep(BLOB_POLL_S)
        if sink.put_blob(sha, data):
            return True
    return False


def blob(
    data: bytes,
    sink: BlobSink | None,
    total: int | None = None,
    *,
    wait: bool = False,
    scrub: Sequence[str] = (),
) -> dict[str, object]:
    """Record a model request or reply: its full content goes to the side folder by hash.

    total is the true size when only part of the content was kept while it went by.
    """
    text = redact(data.decode("utf-8", "replace"), scrub)
    raw = text.encode()
    size = max(len(raw), total or 0)
    kept = raw[:BLOB_CAP]
    sha = hashlib.sha256(kept).hexdigest()
    entry: dict[str, object] = {"sha256": sha, "bytes": size, "preview": text[:PREVIEW]}
    if size > BLOB_CAP:
        entry["cut"] = True
    if sink is not None and not handed_over(sink, sha, kept, wait=wait):
        entry["unsaved"] = True
    return entry


def kept_blob(data: bytes, sink: BlobSink | None, *, wait: bool = False) -> dict[str, object]:
    """A side-folder item whose every string is already cleaned, so it is kept exactly:
    redacting the JSON text again could change what a mock built from it gives back."""
    kept = data[:BLOB_CAP]
    sha = hashlib.sha256(kept).hexdigest()  # the name of what is stored, as blob does
    preview = data[: PREVIEW * 4].decode("utf-8", "ignore")[:PREVIEW]
    entry: dict[str, object] = {"sha256": sha, "bytes": len(data), "preview": preview}
    if len(data) > BLOB_CAP:
        entry["cut"] = True
    if sink is None or not handed_over(sink, sha, kept, wait=wait):
        entry["unsaved"] = True
    return entry


@contextmanager
def inside(step: Step) -> Generator[None]:
    """While a real tool call runs, the calls it makes name it as their parent."""
    if step.call_id is None:
        yield
        return
    token = PARENT.set((os.getpid(), step.call_id))
    try:
        yield
    finally:
        PARENT.reset(token)


_current: RunState | None = None
_process: ProcessState | None = None


def install(state: "RunState | None") -> None:
    """The process slot: a function's or a script's one run, and its process."""
    global _current, _process
    _current = state
    _process = None if state is None else state.process


def install_process(process: ProcessState | None) -> None:
    """A service's process, which has no run of its own."""
    global _current, _process
    _current = None
    _process = process


def process() -> ProcessState | None:
    return _process


def in_service() -> bool:
    return _process is not None and _process.service


# Threads the team started while its code belonged to a run, and that run's name. A map, not
# the thread's name, so the team's logs are unchanged. A pool's own threads are never here.
TAGS: "weakref.WeakKeyDictionary[threading.Thread, str]" = weakref.WeakKeyDictionary()
# A process pool's or queue's own work: a call the process gate let through, while it runs,
# and the threads whose target is named exactly as a pool's or a queue's own, such as a pool's
# handlers and a queue's feeder. Their sends and starts carry work the gate decided at the
# team's call, so the outside rules skip them. No thread is one by inheritance or by default.
_internal = threading.local()
INTERNAL: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()
INTERNAL_TARGETS: set[object] = set()  # thread targets, set by the process gate on import
INTERNAL_KINDS: list[type] = []  # thread classes, such as a process pool's manager thread


def internal_thread(thread: threading.Thread) -> bool:
    target: object = getattr(thread, "_target", None)
    target = getattr(target, "__func__", target)  # a classmethod's target is a bound method
    return target in INTERNAL_TARGETS or isinstance(thread, tuple(INTERNAL_KINDS))


def pool_internal() -> bool:
    if getattr(_internal, "team", False):
        return False
    return getattr(_internal, "depth", 0) > 0 or threading.current_thread() in INTERNAL


@contextmanager
def internal_call(team: bool = False) -> Generator[None]:
    """A pool's own call; team: the team's code that a pool's own thread runs, such as a
    callback, which is never the pool's own."""
    was = getattr(_internal, "team", False), getattr(_internal, "depth", 0)
    _internal.team, _internal.depth = team, was[1] + 1
    try:
        yield
    finally:
        _internal.team, _internal.depth = was


class NoSink:
    """Where a stand-in's own line goes: nowhere, as the runs it concerns have theirs."""

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        del line, keep


def worker_name() -> str:
    """The thread or asyncio task this code runs in, as a message names it."""
    task = None
    current_task = getattr(sys.modules.get("asyncio"), "current_task", None)
    if current_task is not None:
        try:
            task = current_task()
        except RuntimeError:  # no loop in this thread
            task = None
    if task is not None:
        return f"asyncio task {task.get_name()!r}"
    return f"thread {threading.current_thread().name!r}"


def stand_in_plan(run: str, like: Plan | None, real: tuple[str, ...] = ()) -> Plan:
    """A plan that mocks, faults and budgets nothing: what a stand-in decides by. It keeps
    the model hosts, so a model call is still told from a web request."""
    hosts = () if like is None else like.model_hosts
    return Plan(run, "outside", "outside", (), (), (), real, math.inf, model_hosts=hosts)


class Orphan(RunState):
    """Code in a thread a run started, whose context was lost: every call is refused, and
    its line goes to that run while its record is open, which makes the run an
    infrastructure error. Its plan is not recovered from the tag: that would hide the loss.
    """

    def __init__(self, process: ProcessState, name: str, run: RunState | None) -> None:
        plan = None if run is None else run._plan
        super().__init__(stand_in_plan(name, plan), NoSink(), [], process=process)
        self.target = run
        self.name = name

    def decide(self, call: engine.Call, args: dict[str, object]) -> Step:
        step = Step(call, engine.Decision("refused", reason="orphan"), args, now_iso(), 0.0)
        step.started = time.monotonic()
        with self._lock:
            self.refused = self.refused or call.label
            return self._going(step)

    def refusal(self, step: Step, detail: str = "") -> ScrimmageRefused:
        del detail
        return ScrimmageRefused(
            f"{step.call.label}: this call was made in a thread run {self.name} started, but "
            "outside that run's context, so it is refused; the run is an infrastructure error"
        )

    def put(self, line: dict[str, object], *, keep: bool) -> None:
        target = self.target
        if target is not None and target.refused != CLOSED:
            line.update(seq=target.next_seq(), proc=target.proc)
            target.put(line, keep=keep)
            return
        with self.process.lock:
            told = self.name in self.process.orphans_told
            self.process.orphans_told.add(self.name)
        if not told:
            print(
                f"scrimmage: a call in a thread run {self.name} started came after its record "
                "closed, and is refused",
                file=sys.stderr,
            )

    def _first_line(self) -> None:
        """A stand-in says nothing about the process: the run's own first line did."""


def is_write(call: engine.Call) -> bool:
    """A call that can change something: a tool, an MCP tool, a web write, a process."""
    if call.kind == "http":
        return call.method not in engine.READ_METHODS
    return call.kind in ("tool", "mcp", "process")


def outside_entry(call: engine.Call) -> str:
    """The background_real entry that lets this call through."""
    if call.kind == "http":
        return f"{call.label} {call.method} {call.name}"
    return call.label


class Outside(RunState):
    """Code outside any request, in a thread no run started, while runs are open. A write is
    refused unless background_real lists it; a read or a model call runs, and each open run
    is told, as the call could have been one of theirs. One rule, with no switch."""

    def __init__(self, process: ProcessState, runs: list[RunState]) -> None:
        like = runs[0]._plan
        background = () if like is None else like.background_real
        super().__init__(stand_in_plan("outside", like, ("tool:*",)), NoSink(), [], process=process)
        self.runs = runs
        self.background = background

    def decide(self, call: engine.Call, args: dict[str, object]) -> Step:
        label = outside_entry(call)
        listed = labels.matches_real(list(self.background), call.label, call.method, call.name)
        if listed:
            self.tell("background", call, f"{label} ran outside any request, under background_real")
            return super().passed(call)
        if is_write(call):
            message = (
                f"{label} was made outside any request while this run was open, so it is "
                f'refused; add "{label}" to [agent] background_real if it is safe'
            )
            for run in self.runs:
                run.outside_refused(call, message, label)
            step = Step(call, engine.Decision("refused", reason="orphan"), args, now_iso(), 0.0)
            step.started = time.monotonic()
            with self._lock:
                return self._going(step)
        self.tell(
            "lost_context",
            call,
            f"{label} was made outside any request while this run was open, so this run's "
            f'record may not be whole; add "{label}" to [agent] background_real if it is safe',
        )
        return super().passed(call)

    def passed(self, call: engine.Call) -> Step:
        """A read the gate lets through undecided, such as an MCP resource: still told."""
        label = outside_entry(call)
        self.tell(
            "lost_context", call, f"{label} was made outside any request while this run was open"
        )
        return super().passed(call)

    def tell(self, kind: str, call: engine.Call, message: str) -> None:
        extra: dict[str, object] = {"call": call.label, "background_entry": outside_entry(call)}
        for run in self.runs:
            run.notice(kind, "scrimmage", message, extra=extra)

    def refusal(self, step: Step, detail: str = "") -> ScrimmageRefused:
        del detail
        label = outside_entry(step.call)
        return ScrimmageRefused(
            f"{label} was made outside any request while a test run was open, so it is "
            f'refused; add "{label}" to [agent] background_real if it is safe'
        )


def current() -> RunState | None:
    """The run this code belongs to, read at every gated call.

    In a service, the slot: a run's state, or None for a plain request. Code outside any
    request gets the tagged run's orphan stand-in in a thread a run started, else the
    outside rules while any run is open, else None. A function or a script keeps its one run
    in the process slot.
    """
    slot = SLOT.get()
    if isinstance(slot, Joined):
        run = slot.state
        if run.refused == CLOSED and run.entry_done:
            run.process.stale_worker = worker_name()
        return run
    if is_plain(slot):
        return None
    process = _process
    if process is None or not process.service:
        return _current
    tag = TAGS.get(threading.current_thread())
    if tag is not None:
        with process.lock:
            target = process.runs.get(tag)
        return Orphan(process, tag, target)
    runs = process.open_runs()
    return Outside(process, runs) if runs else None
