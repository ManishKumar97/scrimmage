"""Anthropic as a ModelProvider.

A class that holds Anthropic's functions from provider_anthropic, bound once at import, and
adapts the few whose signature the interface changes. Anthropic has one model API, the Messages
API, so no function here decides by path which API a call is. The faults that change or build a
reply are not done yet: each refuses, naming Anthropic, and nothing is guessed.
"""

from collections.abc import Mapping, Sequence

from scrimmage import provider_anthropic as table
from scrimmage.changes import Unchangeable, WebChanged
from scrimmage.config import Fault
from scrimmage.prices import Rates
from scrimmage.providers.base import Fields, Given, ReplyChange


class NotYet:
    """A change to an Anthropic reply: not done yet, so the row is a mistake in the test."""

    def __init__(self, word: str) -> None:
        self.word = word

    def blocked(self, status: int, headers: Sequence[tuple[str, str]]) -> Unchangeable | None:
        del status, headers
        return Unchangeable(
            f"is an Anthropic call, and {self.word} is {table.NOT_YET}; use a status, bad body, "
            "context too long or timeout on this one",
            "unsupported",
        )

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


class Anthropic:
    name = "anthropic"
    display_name = "Anthropic"
    hosts = table.HOSTS
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

    def price(self, model: str, overrides: dict[str, Rates]) -> Rates | None:
        return table.price(model, overrides)

    def cost(
        self,
        tokens_in: int,
        tokens_out: int,
        rates: Rates,
        cached: int = 0,
        writes: Mapping[str, int] | None = None,
    ) -> float:
        short, long = self.writes_of(writes)
        return table.cost(tokens_in, tokens_out, rates, cached, short, long, writes)

    def dearest(
        self,
        rates: Rates,
        tokens_in: int,
        tokens_out: int,
        cached: int,
        writes: Mapping[str, int],
    ) -> Rates:
        short, long = self.writes_of(writes)
        return table.dearest(rates, tokens_in, tokens_out, cached, short, long, writes)

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
    tool_call_tools = staticmethod(table.tool_call_tools)
    no_free_tool_call = staticmethod(table.no_free_tool_call)
    refusal_reply = staticmethod(table.refusal_reply)
    unknown_tool_reply = staticmethod(table.unknown_tool_reply)

    @staticmethod
    def given(path: str, wire: Fields, reply: bytes, *, ids_only: bool) -> Given:
        """Nothing is built or changed yet, so no faulted reply gave the agent anything."""
        del path, wire, reply, ids_only
        return Given()

    @staticmethod
    def carries(path: str, wire: Fields, kept: Sequence[Given]) -> bool:
        del path, wire, kept
        return False

    # ReplyReader
    parse_reply = staticmethod(table.parse_reply)
    called_tool = staticmethod(table.called_tool)
    said = staticmethod(table.said)

    # ReplyChanger
    @staticmethod
    def change(word: str, op: str, path: str) -> ReplyChange:
        del op, path
        return NotYet(word)
