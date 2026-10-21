"""Model adapters: the loop talks to ModelClient, each provider has one implementation."""

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

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
FINISH_REASONS = ("stop", "tool_calls", "length", "content_filter", "refusal")


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
    finish_reason: str
    tokens: int
    # The provider's own form of this turn. It is sent back unchanged on the next call,
    # because some providers (Anthropic thinking blocks) reject a rebuilt turn.
    raw: Any = None


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
    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ChatCompletionFunctionToolParam],
    ) -> Reply: ...


class OpenAIModel:
    """Any OpenAI-compatible chat completions endpoint. Groq is the default."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

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
            finish_reason=choice.finish_reason,
            tokens=response.usage.total_tokens if response.usage else 0,
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
                messages=self.wire_messages(messages),
                tools=[self.wire_tool(tool) for tool in tools],
            )
        except anthropic.APIError as exc:
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
            tokens=response.usage.input_tokens + response.usage.output_tokens,
            raw=response.content,
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


def finish_reason(stop_reason: str | None) -> str:
    match stop_reason:
        case "tool_use":
            return "tool_calls"
        case "max_tokens":
            return "length"
        case "refusal":
            return "refusal"
        case "pause_turn":
            # Only server-side tools pause a turn, and none are sent.
            raise ModelError("the model paused the turn, which needs a server tool")
        case _:
            return "stop"


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
            settings.model or "claude-haiku-5-5",
        )
    return OpenAIModel(
        AsyncOpenAI(
            api_key=settings.groq_api_key.get_secret_value(),
            base_url=settings.groq_base_url,
            max_retries=5,
        ),
        settings.model or "openai/gpt-oss-120b",
    )
