"""A service run's slot in the threads and pools its code uses.

A thread the team starts inside a run carries that run; a pool's thread never does, and each
pool task carries its own submitter's value. Code outside any request follows the outside
rules while a run is open: a write is refused unless background_real lists it, and a read or
a model call runs with a notice. Run on 3.11 to 3.14, and on 3.14 with
-X thread_inherit_context=1.
"""

import asyncio
import contextvars
import multiprocessing.pool
import re
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio
import anyio.to_thread
import httpx2
import pytest

from scrimmage_core import http_gate, process_gate, runner, state, threadctx, tool_gate
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.plan import Plan
from test_http_gate import Budget
from test_service_units import Sink, sink_of

PLAIN = state.Plain()  # one plain request, still being served

INHERITS = threadctx.inherits()
TEAM: contextvars.ContextVar[str] = contextvars.ContextVar("team_variable", default="unset")


@pytest.fixture
def service() -> Iterator[state.ProcessState]:
    process = state.ProcessState(service=True)
    process.stop = lambda message: pytest.fail(message)
    process.listening = True
    state.install_process(process)
    threadctx.install(process)
    process_gate.install_service()  # as start_service does: the pool patches sit together
    process_gate.start_tracker()  # as a process that serves does, before any request
    yield process
    state.install_process(None)


@pytest.fixture
def web(service: state.ProcessState) -> Iterator[None]:
    http_gate.patch(httpx2, http_gate.Context(service, Budget(), None, service.stop))
    yield


def a_run(
    process: state.ProcessState,
    name: str,
    *,
    real: tuple[str, ...] = (),
    background: tuple[str, ...] = (),
    register: bool = True,
) -> state.RunState:
    plan = Plan(name, "t", "baseline", (), (), (), real, 60.0, background_real=background)
    sink = Sink()
    run = state.RunState(plan, sink, [], blobs=sink, process=process)
    if register:
        process.register(run)
    return run


def joined(run: state.RunState) -> contextvars.Context:
    context = contextvars.copy_context()
    context.run(state.SLOT.set, state.Joined(run.name, run))
    return context


def plain() -> contextvars.Context:
    context = contextvars.copy_context()
    context.run(state.SLOT.set, PLAIN)
    return context


def seen() -> object:
    """What code here belongs to: a run's name, PLAIN, or what current() gives."""
    found = state.current()
    if found is None:
        return state.SLOT.get()
    if isinstance(found, state.Orphan):
        return ("orphan", found.name)
    if isinstance(found, state.Outside):
        return "outside rules"
    return found.name


def in_thread(
    fn: Callable[[], object], thread: Callable[..., threading.Thread] = threading.Thread
) -> object:
    got: list[object] = []
    worker = thread(target=lambda: got.append(fn()))
    worker.start()
    worker.join()
    return got[0]


def tool() -> Callable[[str], str]:
    def write_order(order: str) -> str:
        return f"wrote {order}"

    return tool_gate.gate(write_order, "write_order")


@pytest.mark.design("service.thread_carries_run")
def test_a_thread_started_in_a_run_finds_its_plan(service: state.ProcessState) -> None:
    run = a_run(service, "run-a")
    assert joined(run).run(in_thread, seen) == "run-a"


@pytest.mark.design("service.pool_carries_run")
def test_a_pool_task_and_run_in_executor_find_their_plan(service: state.ProcessState) -> None:
    run = a_run(service, "run-a")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(seen).result()  # startup code starts the pool's thread, outside any run
        assert joined(run).run(lambda: pool.submit(seen).result()) == "run-a"

        async def via_loop() -> object:
            return await asyncio.get_running_loop().run_in_executor(pool, seen)

        assert joined(run).run(asyncio.run, via_loop()) == "run-a"


async def run_then_plain(run: state.RunState, how: Callable[[], Any]) -> list[object]:
    """The run's task starts the pool's one thread; a plain task then reuses it."""
    got: list[object] = []
    for slot in (state.Joined(run.name, run), PLAIN):
        token = state.SLOT.set(slot)
        try:
            got.append(await how())
        finally:
            state.SLOT.reset(token)
    return got


@pytest.mark.design("service.pool_thread_never_keeps_a_run")
def test_a_plain_request_in_a_pool_thread_first_started_by_a_run_is_untouched(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    with ThreadPoolExecutor(max_workers=1) as pool:

        async def executor() -> list[object]:
            loop = asyncio.get_running_loop()
            return await run_then_plain(run, lambda: loop.run_in_executor(pool, seen))

        assert asyncio.run(executor()) == ["run-a", PLAIN]

    async def to_thread() -> list[object]:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        return await run_then_plain(run, lambda: asyncio.to_thread(seen))

    assert asyncio.run(to_thread()) == ["run-a", PLAIN]

    async def anyio_sync() -> list[object]:  # FastAPI's sync endpoints take this path
        limiter = anyio.CapacityLimiter(1)
        return await run_then_plain(run, lambda: anyio.to_thread.run_sync(seen, limiter=limiter))

    assert anyio.run(anyio_sync) == ["run-a", PLAIN]


@pytest.mark.design("service.thread_pool_carried")
def test_a_threadpool_task_carries_its_submitters_value_and_its_workers_start_outside(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    before = set(threading.enumerate())
    pool = joined(run).run(multiprocessing.pool.ThreadPool, 1)
    try:
        assert joined(run).run(pool.apply, seen) == "run-a"
        assert joined(run).run(pool.map, lambda _: seen(), [1, 2]) == ["run-a", "run-a"]
        assert plain().run(pool.apply, seen) is PLAIN
        assert plain().run(lambda: list(pool.imap(lambda _: seen(), [1]))) == [PLAIN]
        made = set(threading.enumerate()) - before
        assert made and not any(thread in state.TAGS for thread in made)
    finally:
        pool.terminate()
        pool.join()


@pytest.mark.design("service.pool_thread_never_keeps_a_run")
def test_pool_threads_start_outside_and_are_never_tagged(service: state.ProcessState) -> None:
    run = a_run(service, "run-a")
    before = set(threading.enumerate())
    pool = ThreadPoolExecutor(max_workers=1)
    joined(run).run(lambda: pool.submit(seen).result())
    # A task made in a fresh context runs as the worker's own context, outside any run.
    assert pool.submit(lambda: contextvars.Context().run(state.SLOT.get)).result() is None
    pool.shutdown()

    async def anyio_worker() -> object:
        return await anyio.to_thread.run_sync(lambda: threading.current_thread())

    worker = joined(run).run(anyio.run, anyio_worker)
    made = (set(threading.enumerate()) | {worker}) - before
    assert not any(thread in state.TAGS for thread in made)
    assert isinstance(worker, tuple(threadctx.POOL_THREADS))


@pytest.mark.design("service.startup_outside")
def test_startup_code_submitting_to_a_pool_runs_outside(service: state.ProcessState) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert contextvars.Context().run(lambda: pool.submit(seen).result()) is state.OUTSIDE


@pytest.mark.design("service.plain_thread")
def test_a_thread_started_in_a_plain_request_finds_none(service: state.ProcessState) -> None:
    a_run(service, "run-a")
    assert plain().run(in_thread, state.current) is None


@pytest.mark.design("service.long_plain_request")
def test_a_worker_a_long_plain_request_started_is_plain_until_that_request_ends(
    service: state.ProcessState,
) -> None:
    """A known gap, in docs/limits.md: work a run hands to a thread that a plain request,
    still open, started runs as that plain request, for real. Once the request ends, the
    same work meets the outside rules."""
    import queue

    gated = tool()
    request = state.Plain()
    context = contextvars.copy_context()
    context.run(state.SLOT.set, request)
    jobs: queue.Queue[str] = queue.Queue()
    got: queue.Queue[str] = queue.Queue()

    def worker() -> None:
        while (order := jobs.get()) != "stop":
            try:
                got.put(gated(order))
            except ScrimmageRefused as refused:
                got.put(f"refused: {refused}")

    thread = threading.Thread(target=worker)
    context.run(thread.start)
    a_run(service, "run-a")
    try:
        jobs.put("1")  # a run hands work over while the plain request is still open
        assert got.get(timeout=10) == "wrote 1"
        request.done = True  # the long request, such as a stream, ends
        jobs.put("2")
        assert got.get(timeout=10).startswith("refused: ")
    finally:
        jobs.put("stop")
        thread.join(10)


@pytest.mark.design("service.plain_thread_left_running")
def test_a_thread_a_plain_request_left_running_meets_the_outside_rules_once_it_returns(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    gated = tool()
    request = state.Plain()
    context = contextvars.copy_context()
    context.run(state.SLOT.set, request)
    go, wrote = threading.Event(), threading.Event()
    got: list[str] = []

    def lingering() -> None:
        got.append(gated("1"))  # while the request is served: plain, untouched
        wrote.set()
        go.wait(10)
        try:
            got.append(gated("2"))
        except ScrimmageRefused as refused:
            got.append(f"refused: {refused}")

    worker = threading.Thread(target=lingering)
    context.run(worker.start)
    assert wrote.wait(10)
    request.done = True  # the request's entry call has returned
    go.set()
    worker.join(10)
    assert got[0] == "wrote 1"
    assert got[1].startswith("refused: ") and "outside any request" in got[1]
    assert [x["reason"] for x in sink_of(run).lines] == ["orphan"]


@pytest.mark.design("service.explicit_context_kept")
@pytest.mark.needs_python(min=(3, 14), reason="Thread(context=...) is new in Python 3.14")
def test_an_explicit_context_on_3_14_is_kept_and_a_call_in_it_is_an_orphan(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    given = contextvars.Context()
    given.run(TEAM.set, "mine")

    def make(**kwargs: Any) -> threading.Thread:
        return threading.Thread(context=given, **kwargs)  # type: ignore[call-arg]

    assert joined(run).run(in_thread, lambda: TEAM.get(), make) == "mine"
    assert joined(run).run(in_thread, seen, make) == ("orphan", "run-a")
    gated = tool()

    def write() -> str:
        try:
            return gated("1")
        except ScrimmageRefused as refusal:
            return str(refusal)

    assert "outside that run's context" in str(joined(run).run(in_thread, write, make))
    assert [x["reason"] for x in sink_of(run).lines if "reason" in x] == ["orphan"]


@pytest.mark.design("service.inherit_context_kept")
@pytest.mark.needs_inherit_context
@pytest.mark.skipif(not INHERITS, reason="needs Python 3.14 with -X thread_inherit_context=1")
def test_inherit_context_on_3_14_keeps_the_teams_variables(service: state.ProcessState) -> None:
    run = a_run(service, "run-a")
    context = joined(run)
    context.run(TEAM.set, "mine")
    assert context.run(in_thread, lambda: (TEAM.get(), seen())) == ("mine", "run-a")


@pytest.mark.design("service.thread_no_cycle")
def test_a_thread_started_in_a_request_is_freed_without_the_cyclic_collector(
    service: state.ProcessState,
) -> None:
    """What a thread holds, such as a process pool's named semaphores, must go when the
    thread ends and its last name is dropped, not whenever the collector next runs."""
    import gc
    import weakref

    def start_and_join() -> weakref.ref[threading.Thread]:
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        return weakref.ref(worker)

    gc.collect()
    gc.disable()
    try:
        plain_ref = plain().run(start_and_join)
        outside_ref = contextvars.Context().run(start_and_join)  # started outside any request
        assert plain_ref() is None
        assert outside_ref() is None
    finally:
        gc.enable()


@pytest.mark.design("service.thread_no_cycle")
def test_a_thread_pools_threads_are_freed_without_the_cyclic_collector(
    service: state.ProcessState,
) -> None:
    """A pool's threads start outside any run, a path of their own."""
    import gc
    import weakref

    def made_and_closed() -> list[weakref.ref[threading.Thread]]:
        pool = multiprocessing.pool.ThreadPool(1)
        threads: list[threading.Thread] = [
            *pool._pool,  # type: ignore[attr-defined]
            pool._worker_handler,  # type: ignore[attr-defined]
            pool._task_handler,  # type: ignore[attr-defined]
            pool._result_handler,  # type: ignore[attr-defined]
        ]
        pool.close()
        pool.join()
        return [weakref.ref(t) for t in threads]

    gc.collect()
    gc.disable()
    try:
        refs = plain().run(made_and_closed)
        assert len(refs) == 4
        assert [r for r in refs if r() is not None] == []
    finally:
        gc.enable()


def test_a_start_that_fails_leaves_no_wrapper_on_the_thread(
    service: state.ProcessState,
) -> None:
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    worker = threading.Thread(target=lambda: None)
    # The patched start over a real start that fails.
    patched = threadctx.start_patched(refuse)
    with pytest.raises(RuntimeError):
        plain().run(patched, worker)
    assert "run" not in worker.__dict__


@pytest.mark.design("service.wrapped_once")
def test_a_thread_is_never_wrapped_twice(service: state.ProcessState) -> None:
    start = threading.Thread.start
    threadctx.install(service)
    assert threading.Thread.start is start
    run = a_run(service, "run-a")

    class Restarting(threading.Thread):
        """A thread class whose start goes through Thread.start twice, as some wrappers do."""

        def start(self) -> None:
            threadctx.carry_into(self)  # someone already carried it
            setattr(self, threadctx.MARK, True)
            super().start()

    got = joined(run).run(in_thread, lambda: (seen(), TEAM.get()), Restarting)
    assert got == ("run-a", "unset")


@pytest.mark.design("service.team_variables_untouched")
@pytest.mark.with_inherit_context_flag
@pytest.mark.skipif(INHERITS, reason="under thread_inherit_context the stdlib copies them")
def test_a_thread_started_in_a_run_does_not_carry_the_teams_variables(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    context = joined(run)
    context.run(TEAM.set, "the team's value")
    assert context.run(in_thread, lambda: (TEAM.get(), seen())) == ("unset", "run-a")


@pytest.mark.design("service.orphan_refused")
def test_a_fresh_context_in_a_tagged_thread_is_an_orphan_and_its_write_is_refused(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    other = a_run(service, "run-b")
    gated = tool()

    def lost() -> object:
        try:
            return contextvars.Context().run(gated, "1")
        except ScrimmageRefused as refusal:
            return str(refusal)

    told = joined(run).run(in_thread, lost)
    assert "outside that run's context" in str(told)
    assert [(x["call"], x["reason"]) for x in sink_of(run).lines] == [
        ("tool:write_order", "orphan")
    ]
    assert sink_of(other).lines == []  # the orphan's line is its tagged run's only


@pytest.mark.design("service.logs_unchanged")
def test_the_thread_name_is_unchanged(service: state.ProcessState) -> None:
    run = a_run(service, "run-a")

    def named() -> threading.Thread:
        return threading.Thread(target=lambda: None, name="team-worker")

    worker = joined(run).run(named)
    joined(run).run(worker.start)
    worker.join()
    assert worker.name == "team-worker"
    assert state.TAGS[worker] == "run-a"


@pytest.mark.design("service.outside_write_refused")
def test_a_write_in_a_fresh_context_on_the_loop_thread_while_a_run_is_open_is_refused_in_every_open_run(  # noqa: E501  # the design's name
    service: state.ProcessState,
) -> None:
    runs = [a_run(service, "run-a"), a_run(service, "run-b")]

    async def handler() -> str:  # a fresh context on the loop's own thread
        return contextvars.Context().run(tool(), "1")

    with pytest.raises(ScrimmageRefused, match="background_real"):
        asyncio.run(handler())
    for run in runs:
        line = sink_of(run).lines[0]
        assert (line["call"], line["reason"], line["background_entry"]) == (
            "tool:write_order",
            "orphan",
            "tool:write_order",
        )


CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
REPLY = {
    "id": "c",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def client() -> httpx2.Client:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx2.Response(200, json=REPLY)
        return httpx2.Response(200, json={"ok": True})

    return httpx2.Client(transport=httpx2.MockTransport(answer))


@pytest.mark.design("service.outside_reads_run")
@pytest.mark.usefixtures("web")
def test_an_outside_read_or_model_call_while_a_run_is_open_passes_with_a_lost_context_notice(
    service: state.ProcessState,
) -> None:
    runs = [a_run(service, "run-a"), a_run(service, "run-b")]
    outside = contextvars.Context()
    assert outside.run(client().get, "https://stock.test/items").status_code == 200
    reply = outside.run(client().post, "https://api.openai.com/v1/chat/completions", json=CHAT)
    assert reply.json()["choices"][0]["message"]["content"] == "ok"
    for run in runs:
        notices = [(x["notice"], x["background_entry"]) for x in sink_of(run).lines]
        assert notices == [
            ("lost_context", "http:stock.test GET /items"),
            ("lost_context", "model:openai"),
        ]


@pytest.mark.design("service.in_process_transport")
@pytest.mark.usefixtures("web")
def test_an_outside_post_to_an_app_in_this_process_is_no_write_and_one_to_a_mock_is(
    service: state.ProcessState,
) -> None:
    async def asgi(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"in process"})

    def wsgi(environ: Any, start: Any) -> list[bytes]:
        start("200 OK", [])
        return [b"in process"]

    async def through_asgi() -> str:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(asgi)) as web:
            return (await web.post("http://app.test/orders")).text

    run = a_run(service, "run-a")
    outside = contextvars.Context()
    assert outside.run(asyncio.run, through_asgi()) == "in process"
    wsgi_client = httpx2.Client(transport=httpx2.WSGITransport(wsgi))
    assert outside.run(wsgi_client.post, "http://app.test/orders").text == "in process"
    assert sink_of(run).lines == []
    with pytest.raises(ScrimmageRefused, match="outside any request"):
        outside.run(client().post, "https://stock.test/orders")


@pytest.mark.design("service.background_real")
@pytest.mark.usefixtures("web")
def test_an_outside_write_listed_under_background_real_passes_and_one_under_real_alone_does_not(
    service: state.ProcessState,
) -> None:
    traces = "http:telemetry.test POST /v1/traces"
    run = a_run(service, "run-a", real=("http:audit.test",), background=(traces,))
    outside = contextvars.Context()
    assert outside.run(client().post, "https://telemetry.test/v1/traces").status_code == 200
    with pytest.raises(ScrimmageRefused, match=re.escape('add "http:audit.test POST /log"')):
        outside.run(client().post, "https://audit.test/log")
    assert [x.get("notice") or x.get("reason") for x in sink_of(run).lines] == [
        "background",
        "orphan",
    ]


@pytest.mark.design("service.setup_lists_outside")
@pytest.mark.usefixtures("web")
def test_the_setup_request_lists_every_outside_label_with_its_background_real_entry_and_stops_on_any(  # noqa: E501  # the design's name
    service: state.ProcessState,
) -> None:
    """The SDK's half: every outside call while setup is open leaves its label and the entry
    to add, reads included. The runner reads them and stops the test before any run."""
    setup = a_run(service, "setup-0123456789ab")
    outside = contextvars.Context()
    outside.run(client().get, "https://flags.test/v1/flags")
    with pytest.raises(ScrimmageRefused):
        outside.run(client().post, "https://telemetry.test/v1/traces")
    entries = [x["background_entry"] for x in sink_of(setup).lines]
    assert entries == ["http:flags.test GET /v1/flags", "http:telemetry.test POST /v1/traces"]
    assert runner.outside_entries(sink_of(setup).lines) == entries


@pytest.mark.design("service.background_real_outside_only")
@pytest.mark.usefixtures("web")
def test_background_real_lets_an_outside_call_through_and_never_a_call_inside_a_run(
    service: state.ProcessState,
) -> None:
    traces = "http:telemetry.test POST /v1/traces"
    run = a_run(service, "run-a", background=(traces,))
    with pytest.raises(ScrimmageRefused, match="can change data"):
        joined(run).run(client().post, "https://telemetry.test/v1/traces")
    assert contextvars.Context().run(client().post, "https://telemetry.test/v1/traces")


@pytest.mark.design("service.outside_no_run_untouched")
@pytest.mark.usefixtures("web")
def test_an_outside_write_with_no_run_open_is_untouched(service: state.ProcessState) -> None:
    outside = contextvars.Context()
    assert outside.run(tool(), "1") == "wrote 1"
    assert outside.run(client().post, "https://audit.test/log").status_code == 200
    assert outside.run(state.current) is None


def close(run: state.RunState) -> None:
    """The run's request returned and its record closed."""
    run.entry_done = True
    run.run_closed()
    run.process.unregister(run)


@pytest.mark.design("service.stale_worker")
@pytest.mark.parametrize("kind", ["thread", "task"])
def test_a_lazy_worker_holding_a_closed_run_is_refused_and_the_next_claim_names_it(
    service: state.ProcessState, kind: str
) -> None:
    gated = tool()
    first = a_run(service, "run-first")
    go, done = threading.Event(), threading.Event()
    got: list[object] = []

    def work() -> None:
        go.wait()
        try:
            got.append(gated("1"))
        except ScrimmageRefused as refusal:
            got.append(refusal)
        done.set()

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever)
    loop_thread.start()
    kept: list[asyncio.Task[None]] = []
    try:
        if kind == "thread":
            worker = joined(first).run(lambda: threading.Thread(target=work, name="batcher"))
            joined(first).run(worker.start)
        else:

            async def batcher() -> None:
                await asyncio.get_running_loop().run_in_executor(None, go.wait)
                work()

            def make() -> None:
                kept.append(loop.create_task(batcher(), name="batcher"))

            joined(first).run(loop.call_soon_threadsafe, make)
        close(first)
        go.set()
        assert done.wait(10)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join()
        loop.close()
    assert isinstance(got[0], ScrimmageRefused) and "has ended" in str(got[0])
    assert service.stale_worker is not None and "batcher" in service.stale_worker
    second = a_run(service, "run-next")
    service.claimed(second)
    notice = sink_of(second).lines[0]
    assert notice["notice"] == "structure" and "batcher" in str(notice["message"])
    assert "start it in lifespan" in str(notice["message"])


@pytest.mark.design("service.loop_check")
def test_uvloop_on_3_11_stops_at_the_structure_check_naming_loop_asyncio(
    service: state.ProcessState,
) -> None:
    uvloop = pytest.importorskip("uvloop")
    loop = uvloop.new_event_loop()
    try:
        if sys.version_info < (3, 12):
            assert threadctx.check_loop(loop) == threadctx.LOOP_ADVICE
            assert "--loop asyncio" in threadctx.LOOP_ADVICE
            plain_loop = asyncio.new_event_loop()
            try:
                assert threadctx.check_loop(plain_loop) is None
            finally:
                plain_loop.close()
            return
        assert threadctx.check_loop(loop) is None
        run = a_run(service, "run-uvloop")
        threadctx.watch_loop(loop)

        async def made() -> None:
            await asyncio.sleep(60)

        task = joined(run).run(loop.create_task, made(), name="lingers")
        loop.run_until_complete(asyncio.sleep(0))
        assert threadctx.outlived(run) == ["asyncio task 'lingers'"]
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    finally:
        loop.close()
    zuvloop = sys.modules.get("zuvloop")
    if zuvloop is not None:  # where installed: 3.14 only, so its scan reads contexts
        assert threadctx.check_loop(zuvloop.new_event_loop()) is None


@pytest.mark.design("service.task_scan_safe")
def test_the_task_scan_runs_on_each_loop_while_it_creates_tasks(
    service: state.ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = a_run(service, "run-busy")
    loop = asyncio.new_event_loop()
    threadctx.watch_loop(loop)
    stop = threading.Event()
    made: set[asyncio.Task[None]] = set()

    running = threading.Event()

    async def churn() -> None:
        running.set()
        while not stop.is_set():
            for _ in range(10):
                made.add(loop.create_task(asyncio.sleep(0)))
            await asyncio.sleep(0.01)  # about 1,000 tasks a second

    worker = threading.Thread(target=lambda: loop.run_until_complete(churn()))
    # asyncio.all_tasks retries a set that changed size, so a scan from the wrong thread
    # rarely fails by itself: each scan's thread is checked instead.
    read_on: set[threading.Thread] = set()
    real_all_tasks = asyncio.all_tasks

    def watched(of: Any = None) -> Any:
        read_on.add(threading.current_thread())
        return real_all_tasks(of)

    monkeypatch.setattr(asyncio, "all_tasks", watched)
    worker.start()
    try:
        assert running.wait(10)
        for _ in range(50):
            assert threadctx.outlived(run) == []
        if threadctx.GETS_CONTEXT:
            assert read_on == {worker}
    finally:
        stop.set()
        worker.join()
        loop.close()


@pytest.mark.design("service.outlived_at_close")
def test_a_thread_or_task_that_outlives_its_run_makes_that_run_an_infrastructure_error(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    release = threading.Event()
    worker = joined(run).run(lambda: threading.Thread(target=release.wait, name="lazy"))
    joined(run).run(worker.start)
    loop = asyncio.new_event_loop()
    threadctx.watch_loop(loop)
    loop_thread = threading.Thread(target=loop.run_forever)
    loop_thread.start()
    created = threading.Event()
    lingering: list[asyncio.Task[None]] = []

    def make() -> None:
        lingering.append(loop.create_task(asyncio.sleep(60), name="lingers"))
        created.set()

    joined(run).run(loop.call_soon_threadsafe, make)
    assert created.wait(10)
    try:
        threadctx.close_run(run)
        messages = [str(x["message"]) for x in sink_of(run).lines]
        assert any("thread 'lazy' started in this run and outlived it" in m for m in messages)
        assert any("asyncio task 'lingers' started in this run" in m for m in messages)
        assert runner.context_lost(sink_of(run).lines) is False  # structure: its own rule
    finally:
        release.set()
        worker.join()

        def cancel_all() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_later(0.05, loop.stop)

        loop.call_soon_threadsafe(cancel_all)
        loop_thread.join()
        loop.close()


@pytest.mark.design("service.grace_is_the_runs")
def test_a_call_in_the_grace_window_or_a_background_task_sets_no_stale_flag(
    service: state.ProcessState,
) -> None:
    run = a_run(service, "run-a")
    gated = tool()
    # In the grace window the record is open: the call is the run's own.
    with pytest.raises(ScrimmageRefused, match="neither mocked"):
        joined(run).run(gated, "1")
    # A background task that runs after the reply, before the entry call returned.
    late = a_run(service, "run-b")
    late.run_closed()
    with pytest.raises(ScrimmageRefused, match="has ended"):
        joined(late).run(gated, "1")
    assert service.stale_worker is None


@pytest.mark.design("service.plain_untouched_when_stale")
def test_a_plain_request_is_untouched_while_a_lazy_worker_is_stale(
    service: state.ProcessState,
) -> None:
    service.stale_worker = "thread 'batcher'"
    a_run(service, "run-a")
    assert plain().run(tool(), "1") == "wrote 1"


@pytest.mark.design("service.lost_context_infra")
def test_a_lost_context_notice_makes_the_run_an_infrastructure_error() -> None:
    assert runner.context_lost([{"notice": "lost_context", "call": "model:openai"}])
    assert runner.context_lost([{"action": "refused", "reason": "orphan"}])
    assert not runner.context_lost([{"notice": "background"}, {"reason": "unlisted"}])


@pytest.mark.design("service.outside_write_refused")
def test_an_outside_write_while_a_run_is_open_is_refused_in_every_open_run_and_one_with_none_open_runs(  # noqa: E501  # the design's name
    service: state.ProcessState,
) -> None:
    runs = [a_run(service, "run-a"), a_run(service, "run-b")]
    gated, outside = tool(), contextvars.Context()
    with pytest.raises(ScrimmageRefused):
        outside.run(gated, "1")
    assert all([x["reason"] for x in sink_of(r).lines] == ["orphan"] for r in runs)
    for run in runs:
        close(run)
    assert outside.run(gated, "1") == "wrote 1"
