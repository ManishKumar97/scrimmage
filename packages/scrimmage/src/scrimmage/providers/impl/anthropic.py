"""Anthropic as a ModelProvider.

A class that holds Anthropic's functions from provider_anthropic, bound once at import, and
adapts the few whose signature the interface changes. Anthropic has one model API, the Messages
API, so no function here decides by path which API a call is. The faults that change or build a
reply are built or changed in anthropic_changes, except unknown tool, which is not done yet: it
refuses, naming Anthropic, and nothing is guessed.
"""

from collections.abc import Mapping, Sequence

from scrimmage import provider_anthropic as table
from scrimmage.changes import Unchangeable, WebChanged
from scrimmage.config import Fault
from scrimmage.prices import Rates
from scrimmage.providers.base import Fields, ReplyChange, Usage
from scrimmage.providers.impl import anthropic_changes


class NotYet:
    """A change to an Anthropic reply: not done yet, so the row is a mistake in the test."""

    def __init__(self, word: str) -> None:
        self.word = word

    def blocked(self, status: int, headers: Sequence[tuple[str, str]]) -> Unchangeable | None:
        del status, headers
        why = table.never_carried_out(self.word) or (
            f"is an Anthropic call, and {self.word} is {table.NOT_YET}; use a status, bad body, "
            "context too long or timeout on this one"
        )
        return Unchangeable(why, "unsupported")

    def __call__(
        self,
        status: int,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        fault: Fault,
        *,
        decoded: bool,
    ) -> WebChanged | Unchangeable:
        del body, fault, decoded
        said = self.blocked(status, headers)
        assert said is not None
        return said


class AnthropicChange:
    """One change to an Anthropic reply, for one fault word: every call is the Messages API."""

    def __init__(self, word: str, op: str) -> None:
        self.word, self.op = word, op

    def blocked(self, status: int, headers: Sequence[tuple[str, str]]) -> Unchangeable | None:
        return anthropic_changes.reply_kind(status, self.op, headers, self.word)

    def __call__(
        self,
        status: int,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        fault: Fault,
        *,
        decoded: bool,
    ) -> WebChanged | Unchangeable:
        make = anthropic_changes.model_change(self.word)
        return make(status, self.op, headers, body, fault, decoded=decoded)


class Anthropic:
    name = "anthropic"
    display_name = "Anthropic"
    hosts = table.HOSTS
    fees = (table.SEARCH,)
    # Scrimmage ships no Anthropic prices yet, so the line names where they are and no number.
    price_example = (
        "{input = ..., output = ...} (dollars per million tokens, from "
        "https://platform.claude.com/docs/en/about-claude/pricing)"
    )
    free_ops = table.FREE_OPS

    # Recognizer
    op_for = staticmethod(table.op_for)
    request_fields = staticmethod(table.request_fields)
    model_name = staticmethod(table.model_name)
    offered = staticmethod(table.offered)
    carries_text = staticmethod(table.carries_text)
    streams = staticmethod(table.streams)

    # Meter
    usage = staticmethod(table.usage)
    output_cap = staticmethod(table.output_cap)
    request_tier = staticmethod(table.request_tier)
    gateway_cost = staticmethod(table.gateway_cost)

    @staticmethod
    def snapshot_family(model: str) -> str:
        """The table module's one rule, which its own price lookup reads too."""
        return table.snapshot_family(model)

    def price(
        self, model: str, overrides: dict[str, Rates], fees: Mapping[str, float] | None = None
    ) -> Rates | None:
        return table.price(model, overrides, fees)

    def cost(self, rates: Rates, used: Usage) -> float:
        short, long = self.writes_of(used.writes)
        attempts, searches = self.extras_of(used)
        return table.cost(
            used.input, used.output, rates, used.cached, short, long, attempts, searches
        )

    def dearest(self, rates: Rates, used: Usage) -> Rates:
        short, long = self.writes_of(used.writes)
        attempts, searches = self.extras_of(used)
        return table.dearest(
            rates, used.input, used.output, used.cached, short, long, attempts, searches
        )

    @staticmethod
    def extras_of(used: Usage) -> tuple[tuple[table.Attempt, ...], int]:
        """The attempts and the searches this provider's own reading of a reply keeps. A plain
        Usage, such as one a test builds from counts alone, has neither."""
        if isinstance(used, table.AnthropicUsage):
            return used.attempts, used.searches
        return (), 0

    @staticmethod
    def writes_of(writes: Mapping[str, int] | None) -> tuple[int, int]:
        """The 5 minute and the 1 hour counts, by the Rates fields that price them."""
        found = writes or {}
        return found.get(table.WRITE_5M, 0), found.get(table.WRITE_1H, 0)

    def most_cost(
        self,
        tokens_in: int,
        tokens_out: int,
        rates: Rates,
        *,
        path: str,
        fields: Fields,
    ) -> float:
        """The request decides, by its cache_control: the path is always the Messages API."""
        del path
        return table.most_cost(tokens_in, tokens_out, rates, fields=fields)

    # FaultMaker
    error_body = staticmethod(table.error_body)
    error_content_type = staticmethod(table.error_content_type)
    fault_headers = staticmethod(table.fault_headers)
    broken_body = staticmethod(table.broken_body)
    context_too_long_body = staticmethod(table.context_too_long_body)
    not_carried_out = staticmethod(table.not_carried_out)
    never_carried_out = staticmethod(table.never_carried_out)
    tool_call_tools = staticmethod(table.tool_call_tools)
    no_free_tool_call = staticmethod(table.no_free_tool_call)
    refusal_reply = staticmethod(table.refusal_reply)
    refusal_form_problem = staticmethod(table.refusal_form_problem)
    unknown_tool_reply = staticmethod(table.unknown_tool_reply)

    @staticmethod
    def given(path: str, wire: Fields, reply: bytes, *, ids_only: bool) -> table.Given:
        """What a faulted Messages reply gave the agent: its toolu_ ids and its message."""
        del path
        return table.given(wire, reply, ids_only=ids_only)

    @staticmethod
    def carries(path: str, wire: Fields, kept: Sequence[table.Given]) -> bool:
        del path
        return table.carries(wire, kept)

    # ReplyReader
    parse_reply = staticmethod(table.parse_reply)
    called_tool = staticmethod(table.called_tool)
    said = staticmethod(table.said)

    # ReplyChanger
    @staticmethod
    def change(word: str, op: str, path: str) -> ReplyChange:
        del path
        if word in anthropic_changes.MESSAGES_CHANGES:
            return AnthropicChange(word, op)
        return NotYet(word)
