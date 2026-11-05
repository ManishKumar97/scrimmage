"""Run state: this process's plan, counters, record and the refused-run poison.

A function or a script is one run per process, so the plan lives in one slot shared by
the whole process.
"""

import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from faultline_sdk import decide as engine
from faultline_sdk.errors import FaultlineRefused, FaultlineTimeLimit
from faultline_sdk.plan import Plan
from faultline_sdk.records import RECORD_VERSION
from faultline_sdk.redact import redact

PREVIEW = 200


class LineSink(Protocol):
    def put(self, line: dict[str, object], *, keep: bool = False) -> None: ...


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


class RunState:
    def __init__(self, plan: Plan, sink: LineSink, allowed: list[str]) -> None:
        self.plan = plan
        self.sink = sink
        self.allowed = allowed
        self.began = time.monotonic()
        self.counts = [0] * len(plan.faults)
        self.refused: str | None = None
        self.proc = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started": now_iso(),
        }
        self._seq = 0
        self._lock = threading.Lock()

    def begin(self, name: str, args: dict[str, object]) -> Step:
        """Count the call and decide what it gets."""
        if time.monotonic() - self.began > self.plan.time_limit_s:
            raise FaultlineTimeLimit(
                f"the run passed its time limit of {self.plan.time_limit_s:g}s"
            )
        call = engine.Call("tool", f"tool:{name}", name)
        with self._lock:
            if self.refused is not None:
                decision = engine.Decision("refused")
            else:
                engine.count(call, self.plan, self.counts)
                decision = engine.decide(call, self.plan, self.counts)
                if decision.action == "refused":
                    self.refused = call.label
        return Step(call, decision, args, now_iso(), time.monotonic())

    def refusal(self, step: Step) -> FaultlineRefused:
        first = self.refused or step.call.label
        if first != step.call.label:
            return FaultlineRefused(
                f"{step.call.label}: this run already refused {first}, so every later call fails"
            )
        return FaultlineRefused(
            f"{step.call.label} is neither mocked by this test nor listed under [agent] real. "
            f'Mock it in the test, or add "{step.call.label}" to real if it is safe '
            "to run for real."
        )

    def finish(
        self,
        step: Step,
        *,
        result: object = None,
        error: BaseException | None = None,
        mistake: str | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        decision = step.decision
        line: dict[str, object] = {
            "v": RECORD_VERSION,
            "run": self.plan.run,
            "row": self.plan.row,
            "seq": seq,
            "proc": self.proc,
            "call": step.call.label,
            "kind": step.call.kind,
            "action": decision.action,
            "args": content(step.args),
            "result": None
            if error is not None or decision.action == "refused"
            else content(result),
            "error": None if error is None else f"{type(error).__name__}: {error}",
            "start": step.start,
            "ms": int((time.monotonic() - step.started) * 1000),
        }
        if decision.fault is not None:
            fault = self.plan.faults[decision.fault]
            line["fault"] = {"do": fault.do, "when": fault.when, "n": decision.n}
        if mistake is not None:
            line["mistake"] = mistake
        self.sink.put(line, keep=decision.action == "refused")


_current: RunState | None = None


def install(state: "RunState | None") -> None:
    global _current
    _current = state


def current() -> RunState | None:
    return _current
