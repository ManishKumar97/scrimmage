"""Model adapters: the loop talks to ModelClient, each provider has one implementation."""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import anthropic
import openai
from anthropic import AsyncAnthropic
from anthropic.types import ToolParam
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnionParam,
)

from faultline.settings import Settings

# Anthropic requires an output cap on every request.
ANTHROPIC_MAX_OUTPUT_TOKENS = 16_000

# What the loop sees as finish_reason, whatever the provider calls it.
type FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "refusal"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON, as the model wrote it
    function: bool = True  # False for a tool kind the loop cannot run


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    tokens: int
    # The provider's own form of this turn. It is sent back unchanged on the next call,
    # because some providers (Anthropic thinking blocks) reject a rebuilt turn.
    raw: Any = None
    # For traces. None when the provider did not report them (not zero).
    # input_tokens includes cached input, as the OpenTelemetry convention says.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    response_model: str | None = None
    response_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool


# The conversation after the system prompt: the question, then each model reply
# followed by the results of the tools it asked for.
Message = str | Reply | list[ToolResult]


class ModelError(Exception):
    """The model API failed: rate limit after retries, oversized request, outage."""


class BadToolCall(ModelError):
    """The provider rejected the model's own tool call as unparseable."""


class ModelClient(Protocol):
    provider: str  # the OpenTelemetry provider name, such as "anthropic"
    model: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ChatCompletionFunctionToolParam],
    ) -> Reply: ...


class OpenAIModel:
    """Any OpenAI-compatible chat completions endpoint. Groq is the default."""

    def __init__(
        self, client: AsyncOpenAI, model: str, provider: str = "openai"
    ) -> None:
        self.client = client
        self.model = model
        self.provider = provider

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ChatCompletionFunctionToolParam],
    ) -> Reply:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=self.wire_messages(system, messages),
                tools=tools,
            )
        except openai.BadRequestError as exc:
            # Groq rejects a reply whose tool call it cannot parse.
            # Any other 400, such as a context overflow, is not the model's fault.
            if exc.code == "tool_use_failed":
                raise BadToolCall(str(exc)) from exc
            raise ModelError(str(exc)) from exc
        except openai.APIError as exc:
            raise ModelError(str(exc)) from exc

        choice = response.choices[0]
        calls = [
            ToolCall(
                call.id,
                call.function.name,
                call.function.arguments,
            )
            if isinstance(call, ChatCompletionMessageFunctionToolCall)
            else ToolCall(call.id, call.custom.name, call.custom.input, function=False)
            for call in choice.message.tool_calls or []
        ]
        return Reply(
            text=choice.message.content or "",
            tool_calls=calls,
            # function_call is the legacy name for tool_calls.
            finish_reason=(
                "tool_calls"
                if choice.finish_reason == "function_call"
                else choice.finish_reason
            ),
            tokens=response.usage.total_tokens if response.usage else 0,
            input_tokens=response.usage.prompt_tokens if response.usage else None,
            output_tokens=response.usage.completion_tokens if response.usage else None,
            # Groq's usage carries this field for models it caches. Absent means unreported.
            cache_read_tokens=(
                response.usage.prompt_tokens_details.cached_tokens
                if response.usage and response.usage.prompt_tokens_details
                else None
            ),
            response_model=response.model,
            response_id=response.id,
            # The response and request types describe the same JSON.
            raw=[call.model_dump() for call in choice.message.tool_calls or []],
        )

    @staticmethod
    def wire_messages(
        system: str, messages: list[Message]
    ) -> list[ChatCompletionMessageParam]:
        wire: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
        for message in messages:
            if isinstance(message, str):
                wire.append({"role": "user", "content": message})
            elif isinstance(message, Reply):
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.text or None,
                        "tool_calls": cast(
                            list[ChatCompletionMessageToolCallUnionParam], message.raw
                        ),
                    }
                )
            else:
                wire.extend(
                    {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                    for r in message
                )
        return wire


class AnthropicModel:
    """Anthropic's native Messages API."""

    provider = "anthropic"

    def __init__(self, client: AsyncAnthropic, model: str) -> None:
        self.client = client
        self.model = model

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ChatCompletionFunctionToolParam],
    ) -> Reply:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=ANTHROPIC_MAX_OUTPUT_TOKENS,
                system=system,
                # One top-level marker. The API puts the breakpoint on the last
                # cacheable block, so it moves forward as the conversation grows and
                # each step reads the earlier steps from cache. Per-block markers
                # would need us to track the last block and stay under four.
                # The cache matches a prefix (tools, system, messages in order), so
                # every earlier byte must repeat: schemas are sorted, turns replayed as received.
                cache_control={"type": "ephemeral"},
                messages=self.wire_messages(messages),
                tools=[self.wire_tool(tool) for tool in tools],
            )
        except (anthropic.APIError, ValueError) as exc:
            # ValueError: the SDK refuses before sending, for example when
            # max_tokens is over a model's non-streaming cap.
            raise ModelError(str(exc)) from exc

        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(b.id, b.name, json.dumps(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return Reply(
            text=text,
            tool_calls=calls,
            finish_reason=finish_reason(response.stop_reason),
            # Anthropic's input_tokens leaves out cached input. The budget counts
            # everything the model processed, so add it back.
            tokens=(
                response.usage.input_tokens
                + (response.usage.cache_read_input_tokens or 0)
                + (response.usage.cache_creation_input_tokens or 0)
                + response.usage.output_tokens
            ),
            raw=response.content,
            input_tokens=(
                response.usage.input_tokens
                + (response.usage.cache_read_input_tokens or 0)
                + (response.usage.cache_creation_input_tokens or 0)
            ),
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens,
            cache_creation_tokens=response.usage.cache_creation_input_tokens,
            response_model=response.model,
            response_id=response.id,
        )

    @staticmethod
    def wire_tool(tool: ChatCompletionFunctionToolParam) -> ToolParam:
        function = tool["function"]
        return {
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {"type": "object"}),
        }

    @staticmethod
    def wire_messages(messages: list[Message]) -> list[Any]:
        wire: list[Any] = []
        for message in messages:
            if isinstance(message, str):
                wire.append({"role": "user", "content": message})
            elif isinstance(message, Reply):
                # Replayed as received, thinking blocks included.
                wire.append({"role": "assistant", "content": message.raw})
            else:
                # All results of one reply go back in one user message.
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r.call_id,
                                "content": r.content,
                                **({"is_error": True} if r.is_error else {}),
                            }
                            for r in message
                        ],
                    }
                )
        return wire


def finish_reason(stop_reason: str | None) -> FinishReason:
    match stop_reason:
        case "tool_use":
            return "tool_calls"
        case "max_tokens" | "model_context_window_exceeded":
            return "length"
        case "refusal":
            return "refusal"
        case "pause_turn":
            # Only server-side tools pause a turn, and none are sent.
            raise ModelError("the model paused the turn, which needs a server tool")
        case "end_turn" | "stop_sequence":
            return "stop"
        case _:
            raise ModelError(f"unknown stop reason {stop_reason!r}")


def make_model(settings: Settings) -> ModelClient:
    """Build the client for settings.provider. This is the only place that picks one."""
    # The SDKs retry rate limits and outages with backoff. The run's deadline
    # cancels those retries too, so they cannot push a run past its time budget.
    if settings.provider == "anthropic":
        return AnthropicModel(
            AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                max_retries=5,
            ),
            settings.anthropic_model,
        )
    return OpenAIModel(
        AsyncOpenAI(
            api_key=settings.groq_api_key.get_secret_value(),
            base_url=settings.groq_base_url,
            max_retries=5,
        ),
        settings.groq_model,
        provider="groq",
    )
