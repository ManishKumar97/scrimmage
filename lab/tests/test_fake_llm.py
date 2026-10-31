import asyncio
import json

import openai
import pytest
from aiohttp import web
from openai import AsyncOpenAI

from faultline_lab import fake_llm

SCRIPT = [
    {"tool_calls": [{"name": "get_logs", "arguments": {"service": "cart"}}]},
    {"text": "ROOT CAUSE SERVICE: cart"},
]


async def serve(script: list[dict], use):
    runner = web.AppRunner(fake_llm.app(script), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    client = AsyncOpenAI(
        api_key="fake", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0
    )
    try:
        return await use(client)
    finally:
        await client.close()
        await runner.cleanup()


def test_replies_follow_the_script_by_the_number_of_assistant_messages():
    async def use(client: AsyncOpenAI):
        first = await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "why?"}]
        )
        again = await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "why?"}]
        )
        call = first.choices[0].message.tool_calls
        assert call
        second = await client.chat.completions.create(
            model="m",
            messages=[
                {"role": "user", "content": "why?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [c.model_dump() for c in call],  # type: ignore[misc]
                },
                {"role": "tool", "tool_call_id": call[0].id, "content": "logs"},
            ],
        )
        return first, again, second

    first, again, second = asyncio.run(serve(SCRIPT, use))

    asked = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == "tool_calls"
    assert asked and asked[0].type == "function"
    assert asked[0].function.name == "get_logs"
    assert json.loads(asked[0].function.arguments) == {"service": "cart"}
    assert again.model_dump() == first.model_dump()
    assert second.choices[0].finish_reason == "stop"
    assert second.choices[0].message.content == "ROOT CAUSE SERVICE: cart"
    assert second.usage and second.usage.total_tokens > 0


def test_a_request_past_the_end_of_the_script_is_a_server_error():
    async def use(client: AsyncOpenAI):
        await client.chat.completions.create(
            model="m",
            messages=[
                {"role": "user", "content": "why?"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "and?"},
            ],
        )

    with pytest.raises(openai.InternalServerError, match="only 1 replies"):
        asyncio.run(serve([{"text": "done"}], use))
