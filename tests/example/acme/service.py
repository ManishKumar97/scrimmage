"""Acme's order bot inside a FastAPI service: what a team runs under uvicorn.

POST /chat takes {message, session} and answers {reply}. The async endpoint hands the bot to
the loop's default executor, as many services do; /chat-sync is a sync endpoint, which
FastAPI runs on anyio's worker threads. When TEST_TOKEN is set, every request must carry
it as a bearer token. /echo answers with what it got, for the plain-request tests;
/hold, /left-running and /release show what a plain request's threads and tasks meet.
"""

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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


@app.post("/lost")
async def lost(ask: Ask) -> dict[str, str]:
    """The bot runs in a fresh, empty context: its model call is outside any run."""
    import contextvars

    def bot() -> str:
        return contextvars.Context().run(answer, ask.message)

    return {"reply": await asyncio.get_running_loop().run_in_executor(None, bot)}


@app.post("/stream")
async def stream(ask: Ask) -> Any:
    from fastapi.responses import StreamingResponse

    async def events() -> AsyncIterator[str]:
        yield f"data: {ask.message}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/background")
async def background(ask: Ask, tasks: BackgroundTasks) -> dict[str, str]:
    """Answers at once; checks the stock half a second after the reply, as an audit job."""
    from acme import tools

    def later() -> None:
        time.sleep(0.5)
        with contextlib.suppress(Exception):  # a refusal: the reply has gone already
            tools.check_inventory("SKU-42")

    tasks.add_task(later)
    reply = await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)
    return {"reply": reply}


@app.post("/dies")
async def dies(ask: Ask, tasks: BackgroundTasks) -> dict[str, str]:
    """Answers; for a question that asks it to, the worker then ends before the run's record
    closes, so its last batch never comes."""
    reply = await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)
    if "die" in ask.message:
        tasks.add_task(lambda: os._exit(1))
    return {"reply": reply}


@app.api_route("/echo", methods=["GET", "POST"])
async def echo(request: Request) -> dict[str, Any]:
    body = await request.body()
    return {
        "headers": [[k, v] for k, v in request.headers.items()],
        "body": body.decode("utf-8", "replace"),
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }


LEFT = {name: threading.Event() for name in ("held", "background", "release", "done")}
SEEN: list[str] = []


def checked(label: str) -> None:
    from acme import tools

    try:
        tools.check_inventory("SKU-42")
        SEEN.append(f"{label}: ran")
    except Exception as exc:  # a refusal
        SEEN.append(f"{label}: {type(exc).__name__}")


async def waited(event: threading.Event) -> None:
    await asyncio.get_running_loop().run_in_executor(None, event.wait, 30)


@app.post("/hold")
async def hold() -> dict[str, Any]:
    """Held open until the thread /left-running left has gone on; says what each part met."""
    LEFT["held"].set()
    await waited(LEFT["done"])
    return {"seen": SEEN}


@app.post("/left-running")
async def left_running(tasks: BackgroundTasks) -> dict[str, str]:
    """Once /hold is in: a thread it waits for, a background task, and a thread it leaves
    running until /release."""
    await waited(LEFT["held"])
    joined = threading.Thread(target=checked, args=("waited for",))
    joined.start()
    joined.join()

    def background() -> None:
        checked("background")
        LEFT["background"].set()

    tasks.add_task(background)

    def lingering() -> None:
        LEFT["release"].wait(30)
        LEFT["background"].wait(30)  # the reply can reach /release before this task runs
        checked("left running")
        LEFT["done"].set()

    threading.Thread(target=lingering, daemon=True, name="left-running").start()
    return {"reply": "started"}


@app.post("/release")
async def release() -> dict[str, str]:
    LEFT["release"].set()
    return {"reply": "released"}
