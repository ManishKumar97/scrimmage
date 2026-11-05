"""Acme's order bot: a hand-written loop over the openai library and its own tools."""

import json
from typing import Any

import openai

from acme import tools
from acme.models import Stock

SPECS = [
    {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
    for name in ("get_order", "check_inventory", "refund_order", "get_stock")
]


def run_tool(name: str, arguments: str) -> str:
    try:
        result = getattr(tools, name)(**json.loads(arguments))
    except Exception as exc:  # the bot tells the model what went wrong
        return f"error: {type(exc).__name__}: {exc}"
    if isinstance(result, Stock):
        return f"{result.sku}: {result.count} in stock"
    return str(result)


def answer(question: str) -> str:
    client = openai.OpenAI(max_retries=0)
    messages: list[Any] = [{"role": "user", "content": question}]
    for _ in range(8):
        reply = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=SPECS,  # type: ignore[arg-type]
        )
        message = reply.choices[0].message
        if not message.tool_calls:
            return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            assert call.type == "function"
            content = run_tool(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return "I could not finish."
