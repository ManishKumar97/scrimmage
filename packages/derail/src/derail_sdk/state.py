"""Run state: this process's plan, counters, record and the refused-run poison.

A function or a script is one run per process, so the plan lives in one slot shared by
the whole process. A child process has no plan until its first gated call, so a short
command that makes no calls costs nothing.
"""

import dataclasses
import hashlib
import json
import os
import shlex
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast

from derail_sdk import decide as engine
from derail_sdk.errors import DerailRefused, DerailStopped, DerailTimeLimit
from derail_sdk.plan import Plan
from derail_sdk.records import RECORD_VERSION
from derail_sdk.redact import redact

PREVIEW = 200
BLOB_CAP = 10 * 1024 * 1024  # a side-folder item keeps its first 10 MB and is marked cut
CLOSED = "a run that has ended"
COMMAND_CAP = 1000  # a process's line keeps this much of its command, which can hold a question


class LineSink(Protocol):
    def put(self, line: dict[str, object], *, keep: bool = False) -> None: ...


class BlobSink(Protocol):
    """The sender: side-folder items and settlements it delivers off the agent's thread."""

    def put_blob(self, sha: str, data: bytes) -> bool: ...

    def put_settle(self, reservation: int, usd: float) -> None: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def content(value: object) -> dict[str, object]:
    """Hash, size and preview of a recorded value, with secrets removed first."""
    text = value if isinstance(value, str) else json.dumps(value, default=repr, sort_keys=True)
    text = redact(text)
    data = text.encode()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "preview": text[:PREVIEW],
    }


@dataclass
class Step:
    """One gated call between its decision and its end."""

    call: engine.Call
    decision: engine.Decision
    args: dict[str, object]
    start: str
    started: float
    extra: dict[str, object] = field(default_factory=dict[str, object])


def this_process() -> dict[str, object]:
    return {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso()}


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
    ) -> None:
        """A child passes no plan: load fetches it, and tools names its gates until then."""
        self.child = plan is None
        self._plan: Plan | None = None
        self._load = load
        self._plan_lock = threading.Lock()
        self.sink = sink
        self.listed = set(tools if plan is None else plan.tool_names())  # names gates will reach
        self.adapted: set[str] = set()  # names a framework's adapter has checked
        self.blocked: dict[str, str] = {}  # names a call could skip a gate by, and why
        self.rechecks: list[Callable[[], None]] = []  # an adapter's check of its tools
        self.allowed = allowed
        self.began = time.monotonic()
        self.refused: str | None = None
        self.model_calls = 0
        self.proc = this_process()
        self._seq = 0
        self._lock = threading.Lock()
        self._unannounced = self.child  # a child says what it is on its first line
        self.live: dict[int, Step] = {}  # calls decided and not yet finished
        if plan is not None:
            self._take(plan)

    def _take(self, plan: Plan) -> Plan:
        # Each process counts its own calls, so "the third call" would mean something
        # different in each. Faults fire only in the program the runner started.
        if self.child and not plan.children:
            plan = dataclasses.replace(plan, faults=())
        if plan.unavailable is not None:
            self.refused = self.refused or "this run's plan"
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
            assert self._load is not None
            return self._take(self._load())

    @property
    def seen(self) -> set[str]:
        """Names a gate reaches, as the model sees them. A request carries only names, so
        one tool that can skip its gate blocks every tool of that name."""
        return (self.listed | self.adapted) - self.blocked.keys()

    def recheck_tools(self) -> None:
        """Bring the names up to date: a class can change after its tools were made."""
        for recheck in self.rechecks:
            recheck()

    def check_time(self) -> None:
        """The time limit counts from when this process took up the run.

        For a function the runner stops the program at run_timeout first, so here it
        is a backstop; it matters where one process outlives its runs.
        """
        if time.monotonic() - self.began > self.plan.time_limit_s:
            raise DerailTimeLimit(f"the run passed its time limit of {self.plan.time_limit_s:g}s")

    def decide(self, call: engine.Call, args: dict[str, object]) -> Step:
        """Count the call and decide what it gets. A poisoned run refuses every call."""
        self.check_time()
        plan = self.plan
        with self._lock:
            if self.refused is not None:
                decision = engine.Decision("refused", reason="poisoned")
            else:
                numbers = self.counters.note(call)
                decision = engine.decide(call, plan, numbers)
                if decision.action == "refused":
                    self.refused = call.label
            return self._going(Step(call, decision, args, now_iso(), time.monotonic()))

    def _going(self, step: Step) -> Step:
        self.live[id(step)] = step
        return step

    def begin(self, name: str, args: dict[str, object]) -> Step:
        return self.decide(engine.Call("tool", f"tool:{name}", name), args)

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

    def refusal(self, step: Step, detail: str = "") -> DerailRefused:
        label = step.call.label
        if self.plan.unavailable is not None:
            return DerailRefused(
                f"{label}: this process has no plan for its run, so every call fails: "
                f"{self.plan.unavailable}"
            )
        if self.refused == CLOSED:
            return DerailRefused(f"{label}: this process's run has ended, so every call fails")
        reason = step.decision.reason or "unlisted"
        first = self.refused or label
        if reason == "poisoned" or first != label:
            return DerailRefused(
                f"{label}: this run already refused {first}, so every later call fails"
            )
        messages = {
            "unlisted": (
                f"{label} is neither mocked by this test nor listed under [agent] real. "
                f'Mock it in the test, or add "{label}" to real if it is safe to run for real.'
                f"{detail}"
            ),
            "web_write": (
                f"{label} {detail} is a web request that can change data, and this test "
                "neither mocks it nor lists it under [agent] real. Mock it in the test, or "
                f'add "{label} {detail}" to real if it is safe to run for real.'
            ),
            "budget": f"{label}: the test's budget cannot cover this call. {detail}",
            "no_price": detail,
            "hosted": detail,
            "unknown_tool_type": detail,
            "unseen_tool": detail,
            "unreadable_body": detail,
            "redirect_off_host": detail,
            "unsupported_loop": detail,
            "ambiguous_label": detail,
            "blind_child": detail,
        }
        return DerailRefused(messages.get(reason, f"{label} is refused: {detail}"))

    def run_closed(self) -> None:
        """The run ended and this process lived on, so nothing it does can be recorded."""
        self.refused = self.refused or CLOSED

    def forked(self) -> None:
        """This process is a copy of its parent, so nothing it holds about the process is true.

        A lock another thread held at the fork would stay locked here for good.
        """
        self._lock = threading.Lock()
        self._plan_lock = threading.Lock()
        self.child = True
        self.proc = this_process()
        self._seq = 0
        self.live = {}
        self._unannounced = True
        if self._plan is not None:
            self._take(self._plan)

    def announce(self) -> None:
        """Say once what this process is: its Python, its command and its parent."""
        self._unannounced = False
        command = redact(shlex.join(sys.orig_argv))[:COMMAND_CAP]
        line = self._head()
        line.update(
            notice="process",
            library="derail",
            message="a process joined the run",
            process={"python": redact(sys.executable), "command": command, "parent": os.getppid()},
        )
        self.sink.put(line, keep=True)

    def stop_open(self) -> None:
        """A call going on when the run is stopped would leave no line, so it gets one here."""
        with self._lock:
            steps, self.live = list(self.live.values()), {}
        for step in steps:
            self._write(step, error=DerailStopped("the run was stopped during this call"))

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
        )
        with self._lock:
            self.refused = self.refused or step.call.label
            self._going(step)
        self.finish(step, error=DerailRefused(message), mistake=message)

    def newer_version(
        self,
        library: str,
        found: tuple[int, int] | None,
        tested: tuple[tuple[int, int], tuple[int, int]],
    ) -> None:
        """Note a library newer than the versions this release was tested with."""
        oldest, newest = tested
        if found is not None and found > newest:
            self.notice(
                "newer_version",
                library,
                f"{library} {found[0]}.{found[1]} is newer than the versions this release was "
                f"tested with ({oldest[0]}.{oldest[1]} to {newest[0]}.{newest[1]})",
            )

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
    ) -> None:
        """A line that is not a call, such as a library version the gate cannot use."""
        self._first_line()
        line = self._head()
        line.update(notice=kind, library=library, message=message)
        if mistake is not None:
            line["mistake"] = redact(mistake)
        if tools is not None:
            line["tools"] = tools
        self.sink.put(line, keep=True)

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

    def _write(
        self,
        step: Step,
        *,
        result: object = None,
        error: BaseException | None = None,
        mistake: str | None = None,
    ) -> None:
        decision = step.decision
        self._first_line()
        line = self._head()
        line.update(
            call=redact(step.call.label),  # a server names its own tools
            kind=step.call.kind,
            action=decision.action,
            args=content(step.args),
            result=None if error is not None or decision.action == "refused" else content(result),
            error=None if error is None else error_text(step.call, error),
            start=step.start,
            ms=int((time.monotonic() - step.started) * 1000),
        )
        if decision.fault is not None:
            fault = self.plan.faults[decision.fault]
            line["fault"] = {"do": fault.do, "when": fault.when, "n": decision.n}
        if decision.mock is not None:
            line["mock"] = decision.mock
        if decision.action == "refused":
            line["reason"] = decision.reason or "unlisted"
        if mistake is not None:
            line["mistake"] = redact(mistake)
        line.update({key: scrub(value) for key, value in step.extra.items()})
        write = step.call.kind == "http" and step.call.method not in engine.READ_METHODS
        # Nothing tells an MCP read from an MCP write, so a real one is kept as a write is.
        write = write or (step.call.kind == "mcp" and decision.action == "real")
        write = write or step.call.kind == "process"
        keep = decision.action == "refused" or step.call.kind == "model" or write
        self.sink.put(line, keep=keep)


def error_text(call: engine.Call, error: BaseException) -> str:
    """A web client's message can quote the URL or a header, so a web line keeps its type."""
    if call.kind == "http" and not isinstance(error, DerailRefused):
        return type(error).__name__
    return redact(f"{type(error).__name__}: {error}")


def scrub(value: object) -> object:
    """Remove secrets from the text a gate puts on a line, such as a model name or a path."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub(item) for item in cast(list[object], value)]
    return value


def blob(data: bytes, sink: BlobSink | None, total: int | None = None) -> dict[str, object]:
    """Record a model request or reply: its full content goes to the side folder by hash.

    total is the true size when only part of the content was kept while it went by.
    """
    text = redact(data.decode("utf-8", "replace"))
    raw = text.encode()
    size = max(len(raw), total or 0)
    kept = raw[:BLOB_CAP]
    sha = hashlib.sha256(kept).hexdigest()
    entry: dict[str, object] = {"sha256": sha, "bytes": size, "preview": text[:PREVIEW]}
    if size > BLOB_CAP:
        entry["cut"] = True
    if sink is not None and not sink.put_blob(sha, kept):
        entry["unsaved"] = True
    return entry


_current: RunState | None = None


def install(state: "RunState | None") -> None:
    global _current
    _current = state


def current() -> RunState | None:
    return _current
