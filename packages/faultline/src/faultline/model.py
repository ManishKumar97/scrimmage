"""The data model every stage shares: runs, steps, faults, findings and verdicts.

A trace adapter builds runs. Checks read them and give findings. Reproduce gives verdicts.
Every type is frozen and holds tuples, so a run a check reads cannot be changed under it.
"""

from dataclasses import dataclass
from enum import StrEnum


class FaultKind(StrEnum):
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    EMPTY_RESULT = "empty_result"
    SCHEMA_MISMATCH = "schema_mismatch"
    MODEL_ERROR = "model_error"
    MODEL_RATE_LIMIT = "model_rate_limit"
    MODEL_TOKEN_LIMIT = "model_token_limit"


class Origin(StrEnum):
    OBSERVED = "observed"  # seen in a trace
    INJECTED = "injected"  # caused by the chaos proxy


class Certainty(StrEnum):
    CERTAIN = "certain"
    HEURISTIC = "heuristic"


class Verdict(StrEnum):
    PASSED = "passed"
    FAILED_LOUDLY = "failed_loudly"
    FAILED_SILENTLY = "failed_silently"


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    call_id: str
    arguments: str
    seconds: float
    result: str
    error_type: str | None = None  # None means the call succeeded
    started: int = 0  # unix nanoseconds
    injected: str | None = None  # the fault spec the proxy applied, else None


@dataclass(frozen=True, slots=True)
class Step:
    number: int
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    text: str
    error_type: str | None = None  # None means the model call succeeded
    started: int = 0  # unix nanoseconds
    tool_calls: tuple[ToolCall, ...] = ()
    injected: str | None = None  # the fault spec the proxy applied, else None

    @property
    def tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


@dataclass(frozen=True, slots=True)
class Run:
    trace_id: str
    model: str | None
    stop_reason: str | None
    question: str | None
    answer: str | None
    seconds: float | None  # None when the root span is missing
    complete: bool  # False when the root span is missing: the process died first
    error_type: str | None = None  # the root span ended in ERROR
    steps: tuple[Step, ...] = ()
    unplaced: tuple[ToolCall, ...] = ()  # tool calls before any step
    content_recorded: bool = True  # False when no span carried message content

    @property
    def tokens(self) -> int:
        return sum(step.tokens for step in self.steps)

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        placed = tuple(call for step in self.steps for call in step.tool_calls)
        return placed + self.unplaced


@dataclass(frozen=True, slots=True)
class Fault:
    kind: FaultKind
    trace_id: str
    step: int | None  # the step number, None when the fault is not in one step
    call_id: str | None  # the tool call, None for a model fault
    tool: str | None
    detail: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class Finding:
    fault: Fault | None  # None for a check that needs no fault, on a run without one
    check: str  # the name of the check that flagged the reaction
    certainty: Certainty
    flagged: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class RunVerdict:
    trace_id: str
    verdict: Verdict
    reason: str
    fault: str | None  # the fault spec the run was under, None for a clean run
