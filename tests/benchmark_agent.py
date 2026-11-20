"""The agent the benchmarks run. It makes many calls of each kind and writes their times.

BENCH_CASES says which kinds, BENCH_BASE is the local server's address, BENCH_OUT is where
the times go. Each time is one call, from before it starts to after it returns, in
nanoseconds; the first few calls of each case, which set up connections, are left out.

BENCH_MODE says what is kept. "samples", the default, keeps every call's time. "batches"
keeps each batch's median, for the budget test to compare a plain process with a gated
one. "trips", inside a run, counts the requests each call makes to the runner on the wire,
off the sender thread: a count, so it needs no clock.

With BENCH_PROFILE set, the calls run under cProfile and the stats go to that file.
"""

import asyncio
import cProfile
import http.client
import json
import os
import statistics
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WARMUP = 10
COUNTS = {
    "tool_sync": 3000,
    "tool_async": 3000,
    "tool_mock": 3000,
    "tool_miss": 3000,
    "http_sync": 1000,
    "http_async": 1000,
    "http_mock": 1000,
    "model_sync": 400,
    "model_async": 400,
    "mcp_async": 400,
}
BATCHES = 12
TRIP_CALLS = 20
SENDER_THREAD = "scrimmage-sender"
CHAT: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Where is order 1234?"}],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "tool_sync",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            },
        }
    ],
}


def tool_sync(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


async def tool_async(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


def tool_mock(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


def tool_miss(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


def measure(call: Callable[[], object], count: int) -> list[int]:
    times: list[int] = []
    for _ in range(count):
        start = time.perf_counter_ns()
        call()
        times.append(time.perf_counter_ns() - start)
    return times


async def measure_async(call: Callable[[], Awaitable[object]], count: int) -> list[int]:
    times: list[int] = []
    for _ in range(count):
        start = time.perf_counter_ns()
        await call()
        times.append(time.perf_counter_ns() - start)
    return times


class RoundTrips:
    """Counts each request to the runner that a gated call waits on, as it goes on the wire.

    It wraps http.client's request, for connections to the runner's port, so a request that
    did not go through the SDK's client is counted too. The sender thread's requests, which
    carry record lines in the background, are left out. Every other thread's count, as an
    async call asks from a worker thread.
    """

    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()
        port = urlsplit(os.environ["SCRIMMAGE_CONTROL_URL"]).port
        real = http.client.HTTPConnection.request

        def counted(conn: http.client.HTTPConnection, *args: Any, **kwargs: Any) -> Any:
            if conn.port == port and threading.current_thread().name != SENDER_THREAD:
                with self.lock:
                    self.count += 1
            return real(conn, *args, **kwargs)

        http.client.HTTPConnection.request = counted  # type: ignore[method-assign, assignment]


class Recorder:
    """Takes each case's calls in the way BENCH_MODE says, and keeps them by case."""

    def __init__(self, mode: str) -> None:
        self.out: dict[str, dict[str, list[int]]] = {}
        self.mode = mode
        self.trips = RoundTrips() if mode == "trips" else None

    def batch(self, name: str) -> int:
        return max(COUNTS[name] // 30, 10)

    def take(self, name: str, call: Callable[[], object]) -> None:
        measure(call, WARMUP)
        if self.mode == "samples":
            self.out[name] = {"samples": measure(call, COUNTS[name])}
        elif self.mode == "batches":
            size = self.batch(name)
            medians = [int(statistics.median(measure(call, size))) for _ in range(BATCHES)]
            self.out[name] = {"batches": medians}
        else:
            assert self.trips is not None
            before = self.trips.count
            measure(call, TRIP_CALLS)
            self.out[name] = {"trips": [self.trips.count - before], "calls": [TRIP_CALLS]}

    async def take_async(self, name: str, call: Callable[[], Awaitable[object]]) -> None:
        await measure_async(call, WARMUP)
        if self.mode == "samples":
            self.out[name] = {"samples": await measure_async(call, COUNTS[name])}
        elif self.mode == "batches":
            size = self.batch(name)
            medians = [
                int(statistics.median(await measure_async(call, size))) for _ in range(BATCHES)
            ]
            self.out[name] = {"batches": medians}
        else:
            assert self.trips is not None
            before = self.trips.count
            await measure_async(call, TRIP_CALLS)
            self.out[name] = {"trips": [self.trips.count - before], "calls": [TRIP_CALLS]}


def case_tool_sync(rec: Recorder) -> None:
    rec.take("tool_sync", lambda: tool_sync("A1"))


def case_tool_async(rec: Recorder) -> None:
    asyncio.run(rec.take_async("tool_async", lambda: tool_async("A1")))


def case_tool_mock(rec: Recorder) -> None:
    rec.take("tool_mock", lambda: tool_mock("A1"))


def case_tool_miss(rec: Recorder) -> None:
    # Its test holds a mock of this tool for order A1 only, so this call matches none. The
    # mock is in a test of its own, so no other case pays for its miss checks.
    rec.take("tool_miss", lambda: tool_miss("Z9"))


def case_http_sync(rec: Recorder) -> None:
    import httpx

    with httpx.Client() as client:
        rec.take("http_sync", lambda: client.get(os.environ["BENCH_BASE"] + "/data"))


def case_http_async(rec: Recorder) -> None:
    import httpx

    async def go() -> None:
        async with httpx.AsyncClient() as client:
            url = os.environ["BENCH_BASE"] + "/data"
            await rec.take_async("http_async", lambda: client.get(url))

    asyncio.run(go())


def case_http_mock(rec: Recorder) -> None:
    import httpx

    with httpx.Client() as client:
        rec.take("http_mock", lambda: client.get("http://mocked.invalid/data"))


def case_model_sync(rec: Recorder) -> None:
    import openai

    client = openai.OpenAI(
        base_url=os.environ["BENCH_BASE"] + "/v1", api_key="bench", max_retries=0
    )
    rec.take("model_sync", lambda: client.chat.completions.create(**CHAT))


def case_model_async(rec: Recorder) -> None:
    import openai

    async def go() -> None:
        client = openai.AsyncOpenAI(
            base_url=os.environ["BENCH_BASE"] + "/v1", api_key="bench", max_retries=0
        )
        await rec.take_async("model_async", lambda: client.chat.completions.create(**CHAT))

    asyncio.run(go())


def case_mcp_async(rec: Recorder) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go() -> None:
        args = os.environ["BENCH_MCP"].split("|")
        server = StdioServerParameters(command=sys.executable, args=args)
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            call = lambda: session.call_tool("search", {"q": "refunds"})  # noqa: E731
            await rec.take_async("mcp_async", call)

    asyncio.run(go())


CASES: dict[str, Callable[[Recorder], None]] = {
    "tool_sync": case_tool_sync,
    "tool_async": case_tool_async,
    "tool_mock": case_tool_mock,
    "tool_miss": case_tool_miss,
    "http_sync": case_http_sync,
    "http_async": case_http_async,
    "http_mock": case_http_mock,
    "model_sync": case_model_sync,
    "model_async": case_model_async,
    "mcp_async": case_mcp_async,
}


def answer(question: str) -> str:
    """The function a run starts: makes every case's calls and writes what they took."""
    del question
    recorder = Recorder(os.environ.get("BENCH_MODE", "samples"))
    profile = cProfile.Profile() if "BENCH_PROFILE" in os.environ else None
    if profile is not None:
        profile.enable()
    for name in os.environ["BENCH_CASES"].split(","):
        CASES[name](recorder)
    if profile is not None:
        profile.disable()
        profile.dump_stats(os.environ["BENCH_PROFILE"])
    Path(os.environ["BENCH_OUT"]).write_text(json.dumps(recorder.out))
    return "ok"
