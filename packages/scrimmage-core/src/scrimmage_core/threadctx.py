# pyright: standard
"""Carry a service run's slot into the threads and pool tasks the run starts.

A thread the team starts while its code belongs to a run is tagged with that run, and gets a
new context that holds only Scrimmage's variables, so the team's own are never changed. A
pool's thread outlives the request that made it, so it never belongs to one: it starts
outside any run, untagged, and each task it runs carries its submitter's value for that task
only. Installed in service mode only: a function or a script shares one process slot across
its threads already.

At a run's close, a thread or an asyncio task that holds the run and outlived it makes the
run an infrastructure error. On 3.12 and later each loop's tasks are read for their context,
on that loop's own thread; on 3.11, which cannot read a task's context, the tasks a run made
are kept as it makes them, which needs the loop's create_task to be asyncio's own.
"""

import contextvars
import functools
import sys
import threading
import weakref
from collections.abc import Callable
from types import ModuleType
from typing import Any

from scrimmage_core import hooks, state
from scrimmage_core.state import OUTSIDE, PLAIN, SLOT, TAGS, Joined, ProcessState, RunState

MARK = "__scrimmage_carried__"
SCAN_WAIT_S = 1.0  # how long a run's close waits for each loop to read its tasks
MISSING = object()
GETS_CONTEXT = sys.version_info >= (3, 12)  # asyncio.Task.get_context
HAS_THREAD_CONTEXT = sys.version_info >= (3, 14)  # Thread(context=...)
LOOP_ADVICE = (
    "this loop's create_task cannot be watched on Python 3.11; start the service with --loop "
    "asyncio"
)
_spawning = threading.local()  # set while a pool starts a thread of its own
POOL_THREADS: list[type] = []  # thread classes that are a pool's own workers
LOOPS: "weakref.WeakSet[Any]" = weakref.WeakSet()  # every event loop requests ran on


def values() -> list[tuple[contextvars.ContextVar[Any], object]]:
    """Scrimmage's variables as this code sees them; one with no value is left out. The slot
    is always there: unset is read as outside any request."""
    found: list[tuple[contextvars.ContextVar[Any], object]] = []
    for var in state.CARRIED:
        value = var.get(MISSING)
        if var is SLOT and value is None:
            value = OUTSIDE
        if value is not MISSING:
            found.append((var, value))
    return found


def own_context(given: list[tuple[contextvars.ContextVar[Any], object]]) -> contextvars.Context:
    """A new context holding only these of Scrimmage's variables."""
    context = contextvars.Context()
    for var, value in given:
        context.run(var.set, value)
    return context


def outside_context(base: contextvars.Context | None = None) -> contextvars.Context:
    """A context like base, or an empty one, with the slot outside any request."""
    context = contextvars.Context() if base is None else base.copy()
    context.run(SLOT.set, OUTSIDE)
    return context


def inherits() -> bool:
    """3.14's -X thread_inherit_context=1: a new thread copies its creator's whole context."""
    return bool(getattr(sys.flags, "thread_inherit_context", 0))


def spawning() -> bool:
    return getattr(_spawning, "depth", 0) > 0


def pool_spawn(function: Callable[..., Any]) -> Callable[..., Any]:
    """A pool's own function: any thread it starts is the pool's, outside any run."""

    @functools.wraps(function)
    def marked(*args: Any, **kwargs: Any) -> Any:
        _spawning.depth = getattr(_spawning, "depth", 0) + 1
        try:
            return function(*args, **kwargs)
        finally:
            _spawning.depth -= 1

    setattr(marked, MARK, True)
    return marked


def carrying(fn: Callable[..., Any]) -> Callable[..., Any]:
    """fn as a pool task: it runs in a copy of the worker's own context with only Scrimmage's
    variables set to the submitter's, so no task inherits the one before it."""
    given = values()

    @functools.wraps(fn)
    def task(*args: Any, **kwargs: Any) -> Any:
        context = contextvars.copy_context()
        for var, value in given:
            context.run(var.set, value)
        return context.run(fn, *args, **kwargs)

    return task


def start_patched(original: Callable[[threading.Thread], None]) -> Callable[..., None]:
    def start(self: threading.Thread) -> None:
        if not state.in_service():
            return original(self)
        if spawning() or isinstance(self, tuple(POOL_THREADS)):
            start_outside(self)
            return original(self)
        slot = SLOT.get()
        if slot is PLAIN and not getattr(self, MARK, False):
            # A plain request's thread stays plain, so the outside rules never reach it.
            setattr(self, MARK, True)
            carry_into(self)
            return original(self)
        if not isinstance(slot, Joined):
            return original(self)
        TAGS[self] = slot.run
        if getattr(self, MARK, False):
            return original(self)
        setattr(self, MARK, True)
        carry_into(self)
        return original(self)

    setattr(start, MARK, True)
    return start


def start_outside(thread: threading.Thread) -> None:
    """A pool's thread starts outside any run, untagged, whatever its creator's slot."""
    if HAS_THREAD_CONTEXT:
        given = getattr(thread, "_context", None)
        if given is None and inherits():
            given = contextvars.copy_context()  # what the stdlib would give it
        thread._context = outside_context(given)  # type: ignore[attr-defined]
        return
    run = thread.run
    context = outside_context()
    thread.run = lambda: context.run(run)  # type: ignore[method-assign]


def carry_into(thread: threading.Thread) -> None:
    """A thread a run started carries Scrimmage's variables, and only those. An explicit
    context, or the whole context the stdlib copies under thread_inherit_context, is kept."""
    if HAS_THREAD_CONTEXT:
        if getattr(thread, "_context", None) is not None or inherits():
            return
        thread._context = own_context(values())  # type: ignore[attr-defined]
        return
    run = thread.run
    context = own_context(values())
    thread.run = lambda: context.run(run)  # type: ignore[method-assign]


def install(process: ProcessState) -> None:
    """The patches, once, in a service process."""
    start = threading.Thread.start
    if not getattr(start, MARK, False):
        threading.Thread.start = start_patched(start)  # type: ignore[method-assign]
    hooks.on_import("concurrent.futures.thread", lambda m: patch_executor(m, process))
    hooks.on_import("anyio._backends._asyncio", lambda m: patch_anyio(m, process))
    hooks.on_import("multiprocessing.pool", lambda m: patch_thread_pool(m, process))
    if not GETS_CONTEXT:
        import asyncio.base_events  # asyncio is imported by every ASGI server already

        patch_create_task(asyncio.base_events)


def patch_executor(module: ModuleType, process: ProcessState) -> None:
    pool: Any = getattr(module, "ThreadPoolExecutor", None)
    submit = getattr(pool, "submit", None)
    if submit is None or not hasattr(pool, "_adjust_thread_count"):
        process.structure(
            "scrimmage",
            "concurrent.futures.ThreadPoolExecutor has no submit or _adjust_thread_count, so a "
            "pool thread could carry one run into the next request",
            process.stop,
        )
        return
    if getattr(submit, MARK, False):
        return
    spawn = pool_spawn(submit)

    @functools.wraps(submit)
    def carried_submit(self: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if not state.in_service():
            return submit(self, fn, *args, **kwargs)
        return spawn(self, carrying(fn), *args, **kwargs)

    setattr(carried_submit, MARK, True)
    pool.submit = carried_submit


def patch_anyio(module: ModuleType, process: ProcessState) -> None:
    worker = getattr(module, "WorkerThread", None)
    if not isinstance(worker, type) or not issubclass(worker, threading.Thread):
        process.structure(
            "anyio",
            "anyio has no WorkerThread, so its worker threads could carry one run into the "
            "next request",
            process.stop,
        )
        return
    if worker not in POOL_THREADS:
        POOL_THREADS.append(worker)


THREAD_POOL_TASKS = ("apply_async", "_map_async", "imap", "imap_unordered")


def patch_thread_pool(module: ModuleType, process: ProcessState) -> None:
    """multiprocessing.pool.ThreadPool: each task carries its submitter's values, and the
    pool's own threads, its workers and its handlers, start outside any run."""
    pool: Any = getattr(module, "Pool", None)
    threads: Any = getattr(module, "ThreadPool", None)
    missing = [
        n for n in (*THREAD_POOL_TASKS, "__init__", "_repopulate_pool") if not hasattr(pool, n)
    ]
    if threads is None or missing:
        process.structure(
            "scrimmage",
            f"multiprocessing.pool has no {', '.join(missing) or 'ThreadPool'}, so a thread "
            "pool's tasks could not carry their run",
            process.stop,
        )
        return
    if getattr(pool.__init__, MARK, False):
        return
    pool.__init__ = pool_spawn(pool.__init__)
    pool._repopulate_pool = pool_spawn(pool._repopulate_pool)
    for name in THREAD_POOL_TASKS:
        setattr(pool, name, thread_pool_task(getattr(pool, name), threads))


def thread_pool_task(method: Callable[..., Any], threads: type) -> Callable[..., Any]:
    @functools.wraps(method)
    def task(self: Any, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if state.in_service() and isinstance(self, threads):
            func = carrying(func)
        return method(self, func, *args, **kwargs)

    setattr(task, MARK, True)
    return task


def patch_create_task(module: ModuleType) -> None:
    """On 3.11: keep each task a run makes, as its context cannot be read later."""
    loop = module.BaseEventLoop
    create = loop.create_task
    if getattr(create, MARK, False):
        return

    @functools.wraps(create)
    def create_task(self: Any, coro: Any, *args: Any, **kwargs: Any) -> Any:
        task = create(self, coro, *args, **kwargs)
        slot = SLOT.get()
        if isinstance(slot, Joined):
            slot.state.tasks.add(task)
        return task

    setattr(create_task, MARK, True)
    loop.create_task = create_task


def check_loop(loop: Any) -> str | None:
    """Why this loop cannot be used, if it cannot: on 3.11 its create_task must be the
    patched one, or a run's tasks would go unseen at its close."""
    if GETS_CONTEXT:
        return None
    return None if getattr(type(loop).create_task, MARK, False) else LOOP_ADVICE


def watch_loop(loop: Any) -> None:
    """A loop requests run on: its tasks are read at each run's close."""
    LOOPS.add(loop)


def outlived(run: RunState) -> list[str]:
    """The threads and tasks that hold this run and outlived it, by name, and a loop that
    did not answer in time, as a run whose record cannot be known whole."""
    found = [
        f"thread {thread.name!r}"
        for thread, name in list(TAGS.items())
        if name == run.name and thread.is_alive() and thread is not threading.current_thread()
    ]
    if not GETS_CONTEXT:
        found += [f"asyncio task {t.get_name()!r}" for t in list(run.tasks) if not t.done()]
        return found
    for loop in list(LOOPS):
        if loop.is_closed():
            continue
        names = tasks_on(loop, run)
        if names is None:
            found.append(f"a loop that did not answer within {SCAN_WAIT_S:g}s")
        else:
            found += names
    return found


def tasks_on(loop: Any, run: RunState) -> list[str] | None:
    """The tasks of one loop that hold the run, read on that loop's own thread, so its task
    set never changes while it is read. None if the loop did not answer in time."""
    import asyncio

    def scan() -> list[str]:
        names: list[str] = []
        for task in asyncio.all_tasks(loop):
            if task.done():
                continue
            slot = task.get_context().get(SLOT)  # type: ignore[attr-defined]  # 3.12 and later
            if isinstance(slot, Joined) and slot.state is run:
                names.append(f"asyncio task {task.get_name()!r}")
        return names

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return scan()
    if not loop.is_running():
        return scan()
    done = threading.Event()
    answer: list[list[str]] = []

    def on_loop() -> None:
        try:
            answer.append(scan())
        finally:
            done.set()

    try:
        loop.call_soon_threadsafe(on_loop)
    except RuntimeError:  # closed between the check and the call
        return []
    if not done.wait(SCAN_WAIT_S) or not answer:
        return None
    return answer[0]


def close_run(run: RunState) -> None:
    """At a run's close: anything that outlived it makes it an infrastructure error, named."""
    for name in outlived(run):
        run.notice("structure", "scrimmage", state.outlived_message(name))
