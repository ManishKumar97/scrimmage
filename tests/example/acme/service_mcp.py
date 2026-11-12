"""Acme's docs bot inside a FastAPI service: it connects to its MCP server and lists the
server's tools once, in its lifespan, before any request, as many services do.

POST /chat offers those tools to the model, and answers {reply}. DOCS_SERVER and DOCS_LOG
say how to start the stdio server, as for acme.docs_bot.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from acme.docs_bot import over_stdio

TOOLS: list[dict[str, Any]] = []


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with over_stdio() as session:
        listed = await session.list_tools()  # at startup: no run is open
        TOOLS[:] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in listed.tools
        ]
        yield


app = FastAPI(lifespan=lifespan)


class Ask(BaseModel):
    message: str
    session: str = ""


def answer(question: str) -> str:
    from openai import OpenAI

    reply = OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
        tools=TOOLS,  # type: ignore[arg-type]
        max_tokens=300,
    )
    return reply.choices[0].message.content or ""


@app.post("/chat")
async def chat(ask: Ask) -> dict[str, str]:
    return {"reply": await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)}
