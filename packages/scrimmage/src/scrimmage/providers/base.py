"""The interface every model provider implements, and the types it passes to the gate.

Small interfaces, so a caller depends on the part it uses: a Recognizer reads a request, a
Meter counts tokens and prices, a FaultMaker builds the replies faults send, and a
ReplyChanger rewrites a real reply. ModelProvider is all four, with a name.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from scrimmage.prices import Rates

CHAT = "chat"  # the api of a chat call, as Recognizer.api names it
RESPONSES = "responses"  # and of a Responses-style call


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    known: bool = False
    cached: int = 0  # input tokens the provider served from its prompt cache
    written_30m: int = 0  # input tokens written to the cache for 30 minutes
    written_12h: int = 0  # and for 12 hours
    tier: str | None = None  # the service tier the reply says it was billed at
    cost: float | None = None  # dollars, if a gateway's reply says what the call cost

    @property
    def writes(self) -> tuple[int, int]:
        """The cache-write counts, in the order Meter.cost takes them."""
        return self.written_30m, self.written_12h


@dataclass(frozen=True)
class Given:
    """What a faulted chat reply gave the agent, in the forms a request built from it carries
    back. ids: the reply's tool call ids, made fresh for each reply, so no other conversation
    holds them. prefixes: for each choice, the faulted request's messages and then the
    message the agent got, with None and empty values left out, kept as their count and
    digest. A run that faults every call keeps a few bytes per fault, not the conversation."""

    ids: frozenset[str] = frozenset()
    prefixes: frozenset[tuple[int, bytes]] = frozenset()


@dataclass
class Offered:
    """The tools a model request offers, sorted."""

    agent: list[str] = field(default_factory=list[str])  # names the agent runs itself
    hosted_reads: list[str] = field(default_factory=list[str])  # provider tools that only read
    hosted_writes: list[str] = field(default_factory=list[str])  # MCP server labels
    unknown: list[str] = field(default_factory=list[str])  # tool types not in the table


class Recognizer(Protocol):
    """Which requests are this provider's, and what they hold."""

    @property
    def hosts(self) -> tuple[str, ...]:
        """The hosts that are this provider's with no model_hosts line."""
        ...

    @property
    def apis(self) -> tuple[str, ...]:
        """The names api() returns: the provider's APIs."""
        ...

    @property
    def free_ops(self) -> tuple[str, ...]:
        """The ops that bill no tokens, so nothing is reserved for them."""
        ...

    def op_for(self, path: str, method: str) -> str | None:
        """The op a request to this path is, or None when it is not a model call."""
        ...

    def api(self, path: str) -> str:
        """Which of the provider's APIs a path is, such as chat or responses."""
        ...

    def request_fields(self, body: bytes) -> dict[str, object] | None: ...

    def model_name(self, fields: dict[str, object] | None) -> str | None: ...

    def offered(self, path: str, fields: dict[str, object]) -> Offered: ...


class Meter(Protocol):
    """Tokens, tiers and what a call costs."""

    def usage(self, body: bytes, *, output_required: bool = True) -> Usage: ...

    def max_output(self, fields: dict[str, object]) -> int | None: ...

    def choices(self, fields: dict[str, object]) -> int: ...

    def request_tier(self, fields: dict[str, object] | None) -> str | None: ...

    def gateway_cost(self, value: object, before: float | None = None) -> float | None: ...

    def price(self, model: str, overrides: dict[str, Rates]) -> Rates | None: ...

    def cost(
        self, tokens_in: int, tokens_out: int, rates: Rates, cached: int = 0, *writes: int
    ) -> float: ...

    def dearest(
        self, rates: Rates, tokens_in: int, tokens_out: int, cached: int, *writes: int
    ) -> Rates: ...

    def most_cost(self, tokens_in: int, tokens_out: int, rates: Rates, *, path: str) -> float: ...


class FaultMaker(Protocol):
    """The replies a fault sends, built as the provider's own would be."""

    def error_body(self, status: int) -> bytes: ...

    def error_type(self, status: int) -> str: ...

    def broken_body(self, path: str, op: str) -> bytes: ...

    def context_too_long_body(self) -> bytes: ...

    def refusal_reply(self, path: str, fields: dict[str, object], tokens_in: int) -> bytes: ...

    def tool_call_tools(self, path: str, fields: dict[str, object]) -> list[str]: ...

    def no_free_tool_call(self, path: str, fields: dict[str, object]) -> str | None: ...

    def unknown_tool_reply(self, fields: dict[str, object], tokens_in: int, tool: str) -> bytes: ...

    def given(self, fields: dict[str, object], reply: bytes, *, ids_only: bool) -> Given: ...

    def carries(self, fields: dict[str, object], kept: Sequence[Given]) -> bool: ...

    def references(self, fields: dict[str, object]) -> list[str]: ...

    def response_ids(self, reply: bytes) -> list[str]: ...


class ReplyChanger(Protocol):
    """What a change to a real reply needs from the provider."""

    def fresh_ids(self, value: dict[str, object], output: list[object]) -> dict[str, object]:
        """The changed reply under new ids in this provider's forms."""
        ...


class ModelProvider(Recognizer, Meter, FaultMaker, ReplyChanger, Protocol):
    @property
    def name(self) -> str: ...
