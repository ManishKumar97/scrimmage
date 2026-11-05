"""Acme's order bot: a hand-written loop over the openai library and its own tools."""

import json
from typing import Any

import openai

from acme import tools
from acme.models import Stock

TOOLS = {
    "get_order": ("Look up an order's status and delivery date.", "order_id"),
    "check_inventory": ("Check how many of a product are in stock, as text.", "sku"),
    "refund_order": ("Refund an order. This moves real money.", "order_id"),
    "get_stock": ("Get the stock record for a product.", "sku"),
}
SPECS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {argument: {"type": "string"}},
                "required": [argument],
            },
        },
    }
    for name, (description, argument) in TOOLS.items()
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
    return loop(openai.OpenAI(max_retries=0), question)


def loop(client: openai.OpenAI, question: str) -> str:
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
