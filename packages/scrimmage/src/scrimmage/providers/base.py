"""The interface every model provider implements, and the types it passes to the gate.

Small interfaces, so a caller depends on the part it uses: a Recognizer reads a request, a
Meter counts tokens and prices, a FaultMaker builds the replies faults send and says what a
faulted reply gave the agent, and a ReplyChanger rewrites a real reply. ModelProvider is all
four, with a name. A provider whose replies are kept on the provider's side also implements
ServerChains.

Nothing here names a provider's API. Where an API differs, the provider decides: the gate
passes the request's path and fields, and the provider says what they mean.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NewType, Protocol, cast, runtime_checkable

from scrimmage.prices import Rates

if TYPE_CHECKING:
    from scrimmage.changes import Unchangeable, WebChanged
    from scrimmage.config import Fault


# Parsed wire data, opaque outside the providers. Fields is a parsed model request body and
# Reply a parsed model reply. Both are an object to the type checker, so a subscript, a .get, an
# `in`, an iteration or an attribute on one is a pyright error in any module that is not a
# provider. Only a provider looks inside, through fields_dict, so the gate cannot decide by a
# wire key or shape.
Fields = NewType("Fields", object)
Reply = NewType("Reply", object)

NO_FIELDS = Fields({})  # what a request with no readable JSON object body holds


def fields_dict(fields: Fields | None) -> dict[str, object]:
    """The request's JSON object, for a provider to read. Empty for None."""
    return {} if fields is None else cast(dict[str, object], fields)


def wrap_fields(value: dict[str, object]) -> Fields:
    """A provider's own dict, as a request's Fields to hand on."""
    return Fields(value)


def reply_value(reply: Reply) -> object:
    """The parsed reply, for a provider to read."""
    return reply


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    known: bool = False
    cached: int = 0  # input tokens the provider served from its prompt cache
    # Input tokens written to the cache, by the field of Rates that prices them, such as
    # {"cache_write_30m": 90}. A provider with no cache writes leaves it empty.
    writes: dict[str, int] = field(default_factory=dict[str, int])
    tier: str | None = None  # the service tier the reply says it was billed at
    cost: float | None = None  # dollars, if a gateway's reply says what the call cost


@dataclass(frozen=True)
class Given:
    """What a faulted reply gave the agent, in the forms a request built from it carries
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
    def free_ops(self) -> tuple[str, ...]:
        """The ops that bill no tokens, so nothing is reserved for them."""
        ...

    def op_for(self, path: str, method: str) -> str | None:
        """The op a request to this path is, or None when it is not a model call."""
        ...

    def request_fields(self, body: bytes) -> Fields | None: ...

    def model_name(self, wire: Fields | None) -> str | None: ...

    def offered(self, path: str, wire: Fields) -> Offered: ...

    def streams(self, wire: Fields | None) -> bool:
        """Whether the request asks for a streamed reply."""
        ...

    def carries_text(self, wire: Fields, texts: Sequence[str]) -> bool:
        """Whether a request holds any of these texts, as the provider's request form lays
        out text. A heuristic."""
        ...


class Meter(Protocol):
    """Tokens, tiers and what a call costs."""

    def usage(self, body: bytes, *, output_required: bool = True) -> Usage: ...

    def output_cap(self, wire: Fields, path: str, default: int) -> int:
        """The most output tokens a call to this path could bill: what the request caps it at,
        else default, times what the API bills it for, such as one reply for each choice."""
        ...

    def request_tier(self, wire: Fields | None) -> str | None: ...

    def gateway_cost(self, value: object, before: float | None = None) -> float | None: ...

    def price(self, model: str, overrides: dict[str, Rates]) -> Rates | None: ...

    def cost(
        self,
        tokens_in: int,
        tokens_out: int,
        rates: Rates,
        cached: int = 0,
        writes: Mapping[str, int] | None = None,
    ) -> float: ...

    def dearest(
        self,
        rates: Rates,
        tokens_in: int,
        tokens_out: int,
        cached: int,
        writes: Mapping[str, int],
    ) -> Rates: ...

    def most_cost(
        self,
        tokens_in: int,
        tokens_out: int,
        rates: Rates,
        *,
        path: str,
        fields: Fields,
    ) -> float:
        """What a call could cost at most: the dearest the provider may bill each token for,
        given the API the path names and what the request asks for."""
        ...


class FaultMaker(Protocol):
    """The replies a fault sends, built as the provider's own would be, and what a faulted
    reply gave the agent."""

    def error_body(self, status: int) -> bytes: ...

    def error_content_type(self, status: int) -> str:
        """The content type of an error reply with this status."""
        ...

    def fault_headers(self, content_type: str) -> dict[str, str]:
        """The headers of a reply a fault builds, as the provider's own replies carry them: the
        content type and the header its client reads a request id from. The gate adds the
        retry-after of a status fault to these."""
        ...

    def broken_body(self, path: str, op: str) -> bytes: ...

    def context_too_long_body(self) -> bytes: ...

    def not_carried_out(self, word: str, path: str, wire: Fields) -> str | None:
        """Why this fault cannot be carried out on this request, which is then a mistake in
        the test, or None when it can."""
        ...

    def refusal_reply(self, path: str, wire: Fields, tokens_in: int) -> bytes: ...

    def tool_call_tools(self, path: str, wire: Fields) -> list[str]: ...

    def no_free_tool_call(self, path: str, wire: Fields) -> str | None: ...

    def unknown_tool_reply(self, wire: Fields, tokens_in: int, tool: str) -> bytes: ...

    def given(self, path: str, wire: Fields, reply: bytes, *, ids_only: bool) -> Given:
        """What this faulted reply to a request to this path gave the agent, in the forms a
        later request carries back. Empty when the API keeps replies on its own side."""
        ...

    def carries(self, path: str, wire: Fields, kept: Sequence[Given]) -> bool: ...


class ReplyReader(Protocol):
    """What a report prints of a recorded reply: what the model said and which tool it called."""

    def called_tool(self, wire: Reply) -> str | None:
        """The name of the first tool the parsed reply calls, or None."""
        ...

    def said(self, wire: Reply, parts: Sequence[object]) -> str | None:
        """The text, or a tool call's arguments, at the first of these parts a change recorded,
        in the provider's own part names, or the reply's first text if none is named."""
        ...

    def parse_reply(self, data: bytes) -> Reply:
        """A recorded reply's bytes, parsed. A reply that cannot be read reads as nothing, so
        the report holds the bytes and never the parsed value."""
        ...


class ReplyChange(Protocol):
    """One change to a real reply, for one fault word on one API."""

    def blocked(self, status: int, headers: Sequence[tuple[str, str]]) -> "Unchangeable | None":
        """None when the reply's headers allow the change, else why not. Decided by the
        headers alone, so a stream is never read to find out."""
        ...

    def __call__(
        self,
        status: int,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        fault: "Fault",
        *,
        decoded: bool,
    ) -> "WebChanged | Unchangeable": ...


class ReplyChanger(Protocol):
    """What a change to a real reply needs from the provider: replace, garble, truncate and
    bad tool args, each as the provider's own replies hold their text."""

    def change(self, word: str, op: str, path: str) -> ReplyChange: ...


class ModelProvider(Recognizer, Meter, FaultMaker, ReplyChanger, ReplyReader, Protocol):
    @property
    def name(self) -> str: ...

    @property
    def display_name(self) -> str:
        """How `scrimmage check` names the provider, such as OpenAI."""
        ...

    def snapshot_family(self, model: str) -> str:
        """The model a dated snapshot belongs to, by this provider's own way of dating one, or
        the name itself: the price of a snapshot is its family's."""
        ...


@runtime_checkable
class ServerChains(Protocol):
    """A provider whose replies are kept on its side, so a later request can name one by id.
    A request that names a reply or an item a fault changed or built asks the provider to
    look up something it never held. Both answer for any path: an API that keeps nothing
    gives an empty list."""

    def chain_ids(self, path: str, reply: bytes) -> list[str]:
        """The ids of this reply and of its items that a later request may name."""
        ...

    def chain_references(self, path: str, wire: Fields) -> list[str]:
        """What this request asks the provider to look up by id."""
        ...

    def chain_broken(self, found: str) -> str:
        """Why a request that looks up this faulted id is a mistake in the test, as it
        follows the call's label."""
        ...
