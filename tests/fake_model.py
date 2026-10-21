"""A scripted stand-in for the OpenAI client, so loop behaviour is testable without a model."""

import asyncio
import json
from typing import cast

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from faultline import ida
from faultline.models import OpenAIModel


def tool_reply(*calls: tuple[str, str]) -> ChatCompletion:
    """A model reply that asks for tools. Each call is (name, raw JSON arguments)."""
    tool_calls = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        for i, (name, arguments) in enumerate(calls)
    ]
    return _completion(
        {"role": "assistant", "content": None, "tool_calls": tool_calls}, "tool_calls"
    )


def logs_call(service: str) -> tuple[str, str]:
    return ("get_logs", json.dumps({"service": service}))


def text_reply(text: str, finish_reason: str = "stop") -> ChatCompletion:
    return _completion({"role": "assistant", "content": text}, finish_reason)


def _completion(message: dict, finish_reason: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "fake",
            "object": "chat.completion",
            "created": 0,
            "model": "fake-model",
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }
    )


def rejected(
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    code: str | None = None,
) -> openai.APIStatusError:
    """The error the SDK raises when Groq answers with this HTTP status."""
    request = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx2.Response(status, headers=headers, request=request)
    error = {
        400: openai.BadRequestError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    body = {"code": code, "message": message}
    return error(message, response=response, body=body)


def run_ida(
    question: str, client: "FakeClient", model_name: str, *args, **kwargs
) -> ida.RunResult:
    """Run the async loop to completion against a fake client."""
    # FakeClient implements only the part of AsyncOpenAI the adapter uses.
    model = OpenAIModel(cast(AsyncOpenAI, client), model_name)
    return asyncio.run(ida.run(question, model, *args, **kwargs))


class FakeClient:
    """Returns scripted replies in order and records every request it gets."""

    def __init__(self, *replies: ChatCompletion | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs) -> ChatCompletion:
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        if not self.replies:
            raise AssertionError(
                "the loop asked the model for more replies than scripted"
            )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def tool_results(self) -> list[str]:
        """Every tool result the loop sent back to the model, in order."""
        last = self.requests[-1]["messages"]
        return [m["content"] for m in last if m["role"] == "tool"]
