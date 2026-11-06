"""Run state: this process's plan, counters, record and the refused-run poison.

A function or a script is one run per process, so the plan lives in one slot shared by
the whole process.
"""

import dataclasses
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast

from faultline_sdk import decide as engine
from faultline_sdk.errors import FaultlineRefused, FaultlineTimeLimit
from faultline_sdk.plan import Plan
from faultline_sdk.records import RECORD_VERSION
from faultline_sdk.redact import redact

PREVIEW = 200
BLOB_CAP = 10 * 1024 * 1024  # a side-folder item keeps its first 10 MB and is marked cut


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


class RunState:
    def __init__(self, plan: Plan, sink: LineSink, allowed: list[str]) -> None:
        self.plan = plan
        self.sink = sink
        self.listed = set(plan.tool_names())  # names the plan's gates will reach
        self.adapted: set[str] = set()  # names a framework's adapter has checked
        self.blocked: dict[str, str] = {}  # names a call could skip a gate by, and why
        self.rechecks: list[Callable[[], None]] = []  # an adapter's check of its tools
        self.allowed = allowed
        self.began = time.monotonic()
        self.counters = engine.Counters(plan)
        self.refused: str | None = None
        self.model_calls = 0
        self.proc = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started": now_iso(),
        }
        self._seq = 0
        self._lock = threading.Lock()

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
            raise FaultlineTimeLimit(
                f"the run passed its time limit of {self.plan.time_limit_s:g}s"
            )

    def decide(self, call: engine.Call, args: dict[str, object]) -> Step:
        """Count the call and decide what it gets. A poisoned run refuses every call."""
        self.check_time()
        with self._lock:
            if self.refused is not None:
                decision = engine.Decision("refused", reason="poisoned")
            else:
                numbers = self.counters.note(call)
                decision = engine.decide(call, self.plan, numbers)
                if decision.action == "refused":
                    self.refused = call.label
        return Step(call, decision, args, now_iso(), time.monotonic())

    def begin(self, name: str, args: dict[str, object]) -> Step:
        return self.decide(engine.Call("tool", f"tool:{name}", name), args)

    def refuse(self, step: Step, reason: str) -> Step:
        """Turn a decided call into a refusal for a reason the gate found, and poison the run."""
        with self._lock:
            self.refused = self.refused or step.call.label
        step.decision = engine.Decision("refused", reason=reason)
        return step

    def refusal(self, step: Step, detail: str = "") -> FaultlineRefused:
        label = step.call.label
        reason = step.decision.reason or "unlisted"
        first = self.refused or label
        if reason == "poisoned" or first != label:
            return FaultlineRefused(
                f"{label}: this run already refused {first}, so every later call fails"
            )
        messages = {
            "unlisted": (
                f"{label} is neither mocked by this test nor listed under [agent] real. "
                f'Mock it in the test, or add "{label}" to real if it is safe to run for real.'
            ),
            "model_host_write": (
                f"{detail} is a write to a model host and not a model call. Faultline "
                "refuses it because it could leave something behind at the provider that no "
                "test cleans up. No setting allows it yet; web-request labels come later."
            ),
            "budget": f"{label}: the test's budget cannot cover this call. {detail}",
            "no_price": detail,
            "hosted": detail,
            "unknown_tool_type": detail,
            "unseen_tool": detail,
            "unreadable_body": detail,
            "redirect_off_host": detail,
            "unsupported_loop": detail,
        }
        return FaultlineRefused(messages.get(reason, f"{label} is refused: {detail}"))

    def drop_faults(self) -> None:
        self.plan = dataclasses.replace(self.plan, faults=())
        self.counters = engine.Counters(self.plan)

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
        self.finish(step, error=FaultlineRefused(message), mistake=message)

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
        line: dict[str, object] = {
            "v": RECORD_VERSION,
            "run": self.plan.run,
            "test": self.plan.test,
            "row": self.plan.row,
            "seq": self.next_seq(),
            "proc": self.proc,
            "notice": kind,
            "library": library,
            "message": message,
        }
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
        decision = step.decision
        line: dict[str, object] = {
            "v": RECORD_VERSION,
            "run": self.plan.run,
            "test": self.plan.test,
            "row": self.plan.row,
            "seq": self.next_seq(),
            "proc": self.proc,
            "call": step.call.label,
            "kind": step.call.kind,
            "action": decision.action,
            "args": content(step.args),
            "result": None
            if error is not None or decision.action == "refused"
            else content(result),
            "error": None if error is None else redact(f"{type(error).__name__}: {error}"),
            "start": step.start,
            "ms": int((time.monotonic() - step.started) * 1000),
        }
        if decision.fault is not None:
            fault = self.plan.faults[decision.fault]
            line["fault"] = {"do": fault.do, "when": fault.when, "n": decision.n}
        if decision.action == "refused":
            line["reason"] = decision.reason or "unlisted"
        if mistake is not None:
            line["mistake"] = redact(mistake)
        line.update({key: scrub(value) for key, value in step.extra.items()})
        self.sink.put(line, keep=decision.action == "refused" or step.call.kind == "model")


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
