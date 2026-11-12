"""Acme's order bot inside a FastAPI service: what a team runs under uvicorn.

POST /chat takes {message, session} and answers {reply}. The async endpoint hands the bot to
the loop's default executor, as many services do; /chat-sync is a sync endpoint, which
FastAPI runs on anyio's worker threads. When TEST_TOKEN is set, every request must carry
it as a bearer token. /echo answers with what it got, for the plain-request tests.
"""

import asyncio
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from acme.agent import answer

app = FastAPI()


class Ask(BaseModel):
    message: str
    session: str = ""


def authorized(request: Request) -> None:
    token = os.environ.get("TEST_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad token")


@app.post("/chat")
async def chat(ask: Ask, request: Request) -> dict[str, str]:
    authorized(request)
    reply = await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)
    return {"reply": reply}


@app.post("/chat-sync")
def chat_sync(ask: Ask, request: Request) -> dict[str, str]:
    authorized(request)
    return {"reply": answer(ask.message)}


@app.post("/chat-blocking")
async def chat_blocking(ask: Ask, request: Request) -> dict[str, str]:
    """Holds its event loop for 300 ms, so a busy worker is less likely to take the next
    connection."""
    authorized(request)
    time.sleep(0.3)
    return {"reply": await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)}


def child_view() -> dict[str, Any]:
    """What a process the team starts from a worker finds: Scrimmage off, no token's path."""
    import sys

    return {
        "scrimmage": "scrimmage_core.state" in sys.modules,
        "token_file": "SCRIMMAGE_TOKEN_FILE" in os.environ,
    }


@app.post("/env")
async def env() -> dict[str, Any]:
    return {"pid": os.getpid(), **child_view()}


@app.post("/spawn")
async def spawn() -> dict[str, Any]:
    """The team's own spawn pool, started from a worker, for a plain request."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return await asyncio.get_running_loop().run_in_executor(
            None, pool.submit(child_view).result
        )


@app.post("/child")
async def child() -> dict[str, str]:
    """A Python child the endpoint starts, as a tool that shells out would."""
    import subprocess
    import sys

    try:
        done = subprocess.run([sys.executable, "-c", "print('child ran')"], capture_output=True)
    except Exception as exc:  # a refusal, as the agent would see it
        return {"reply": f"{type(exc).__name__}: {exc}"}
    return {"reply": done.stdout.decode().strip()}


@app.api_route("/echo", methods=["GET", "POST"])
async def echo(request: Request) -> dict[str, Any]:
    body = await request.body()
    return {
        "headers": [[k, v] for k, v in request.headers.items()],
        "body": body.decode("utf-8", "replace"),
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }
