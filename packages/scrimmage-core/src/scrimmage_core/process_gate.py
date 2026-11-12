"""The gate for child processes, on Python's audit events.

A child on the run's Python that does not inherit Scrimmage's environment, or that is told to
skip the startup file, would run its writes with no gate and leave no line. Python reports
every process start to an audit hook, which can refuse it. Only starts made from Python are
seen: sh -c 'python x', env -i python and an exec from another program are not.

In a service, a run is one request in a process that serves others, so nothing it does may
reach another process: every start under a run is refused unless real lists its program, and
so are a fork, a multiprocessing process, every multiprocessing send, and the process pools
whose sends run on threads of their own. A plain request's are untouched. Code outside any
request meets the outside rules while a run is open: each is a write, refused unless
background_real lists it, except a pool's own threads and stops, which carry work this gate
decided at the team's call.
"""

import functools
import os
import shlex
import shutil
import sys
import threading
import weakref
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from types import ModuleType
from typing import Any, cast

from scrimmage_core import hooks, python_args, state
from scrimmage_core.decide import Call, Decision
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.labels import PROCESS as LABEL
from scrimmage_core.state import (
    Outside,
    RunState,
    carried,
    current,
    in_service,
    internal_call,
    pool_internal,
)

BOOT = os.path.join("scrimmage_core", "_boot")  # the runner's, which may be in another venv
EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn")  # os.spawnv is exec here
# In a service every start is watched, a fork and os.system too. On 3.14 a Popen raises
# _posixsubprocess.fork_exec as well, the same start, decided once.
SERVICE_EVENTS = (*EVENTS, "os.fork", "os.forkpty", "os.system", "_posixsubprocess.fork_exec")
GATED = "__scrimmage_gate__"
_popen = threading.local()  # the argv of a Popen this thread just let through: its start
# The MCP gate decides a stdio server's calls, so its own start of the server is not blind.
EXEMPT: ContextVar[bool] = carried(ContextVar("scrimmage_process_exempt", default=False))


def command(event: str, args: tuple[Any, ...]) -> tuple[str, list[str], str, dict[str, str]]:
    """The program, its arguments, its folder and its environment, for any of the events."""
    if event == "subprocess.Popen":
        program, argv, folder, env = args
    else:
        program, argv, env = args
        folder = None
    named = cast(Mapping[Any, Any], os.environ if env is None else env)
    return (
        os.fsdecode(program),
        [os.fsdecode(arg) for arg in argv],
        "" if folder is None else os.fsdecode(folder),
        {os.fsdecode(key): os.fsdecode(value) for key, value in named.items()},
    )


def is_this_python(path: str) -> bool:
    """Another Python has its own packages, and runs untouched even with the environment.

    A venv elsewhere is another Python, whatever binary it was made from. The binary with no
    venv is this Python, as nothing shows it has other packages.
    """
    folder = os.path.dirname(os.path.abspath(path))
    home = os.path.dirname(folder)
    mine = os.path.realpath(home) == os.path.realpath(sys.prefix)
    if not mine and os.path.exists(os.path.join(home, "pyvenv.cfg")):
        return False
    return mine or os.path.realpath(path) == os.path.realpath(sys.executable)


def is_boot(folder: str) -> bool:
    """Any Scrimmage's startup folder, since the runner's can differ from this process's own."""
    return folder.endswith(os.sep + BOOT) and os.path.isfile(
        os.path.join(folder, "sitecustomize.py")
    )


def why_blind(env: dict[str, str], args: list[str]) -> str | None:
    folders = env.get("PYTHONPATH", "").split(os.pathsep)
    if "SCRIMMAGE_TOKEN" not in env or not any(map(is_boot, folders)):
        return "its environment does not hold Scrimmage's variables and its PYTHONPATH folder"
    flag = python_args.skipping(args)
    return f"{flag} skips Scrimmage's startup file" if flag else None


def install(before_exec: Callable[[], None]) -> None:
    sys.addaudithook(hook_for(before_exec))


def hook_for(before_exec: Callable[[], None]) -> Callable[[str, tuple[Any, ...]], None]:
    """The audit hook. The run is read at each start, so a child is refused or recorded
    against the run that started it, not the first one this process served."""

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if in_service():
            service_hook(event, args)
            return
        if event not in EVENTS:
            return
        run = current()
        if run is None:
            if event == "os.exec":
                before_exec()
            return
        program, argv, folder, env = command(event, args)
        found = program if "/" in program else shutil.which(program, path=env.get("PATH"))
        target = python_args.interpreter(os.path.join(folder, found), env) if found else None
        why = None
        if target is not None and is_this_python(target[0]) and not EXEMPT.get():
            why = why_blind(env, target[1] + argv[1:])
        if why is not None:
            refuse(run, target[0] if target else program, argv, why)
        if event == "os.exec":
            before_exec()

    return hook


def program_of(event: str, args: tuple[Any, ...]) -> tuple[str | None, str]:
    """The base name of the program a start runs, None for a fork, and its command line."""
    if event in ("os.fork", "os.forkpty"):
        return None, event
    if event == "os.system":
        return "sh", os.fsdecode(args[0])
    if event == "_posixsubprocess.fork_exec":
        paths = args[0] if args and args[0] else ()
        given = cast(list[Any], args[1] or []) if len(args) > 1 else []
        argv = [os.fsdecode(a) for a in given]
        first = os.fsdecode(paths[0]) if paths else (argv[0] if argv else "python")
        return os.path.basename(first), shlex.join(argv)
    program, argv, _, _ = command(event, args)
    return os.path.basename(program), shlex.join(argv)


def service_hook(event: str, args: tuple[Any, ...]) -> None:
    """The audit hook's service half: it does nothing outside service mode."""
    if in_service() and event in SERVICE_EVENTS:
        service_start(event, args)


def service_start(event: str, args: tuple[Any, ...]) -> None:
    """A start in a service: refused under a run unless real lists its program."""
    if event in ("_posixsubprocess.fork_exec", "os.posix_spawn") and popen_started(event, args):
        return  # the start its Popen event decided already
    run = current()
    if run is None or (isinstance(run, Outside) and pool_internal()) or tracker(event, args):
        return
    program, line = program_of(event, args)
    if program is None:
        refuse_other(run, "process:fork", "os.fork", "a fork would carry this run into a copy")
        return
    label = f"process:{program}"
    step = run.decide(Call("process", label, program), {"command": line})
    if step.decision.action == "real":
        run.finish(step)
        _popen.argv = (program, argv_of(event, args)) if event == "subprocess.Popen" else None
        return
    refused(run, step, program_fix(program))


def argv_of(event: str, args: tuple[Any, ...]) -> list[str]:
    given: Any = args[1] if event == "_posixsubprocess.fork_exec" else command(event, args)[1]
    items = cast(list[Any], [given] if isinstance(given, (str, bytes)) else list(given or []))
    return [os.fsdecode(a) for a in items]


def popen_started(event: str, args: tuple[Any, ...]) -> bool:
    """This start is the one the Popen this thread just let through makes, by its program
    and its arguments. Either way the Popen is spent, and a Popen that returns spends it
    too, so a start that went another way leaves nothing for a later start to pass on."""
    allowed = getattr(_popen, "argv", None)
    _popen.argv = None
    return allowed == (program_of(event, args)[0], argv_of(event, args))


def patch_popen(module: ModuleType) -> None:
    """subprocess.Popen: once it returns, the start it let through is spent, as on Python
    3.11 to 3.13 its own fork raises no audit event that would spend it."""
    popen: Any = getattr(module, "Popen", None)
    init = getattr(popen, "__init__", None)
    if init is None or getattr(init, GATED, False):
        return

    @functools.wraps(init)
    def spent(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            init(self, *args, **kwargs)
        finally:
            _popen.argv = None

    setattr(spent, GATED, True)
    popen.__init__ = spent


def program_fix(program: str) -> str:
    return (
        f"{program} was started inside a test run, and service runs refuse every process "
        f'start. Add "process:{program}" to [agent] real if it is safe.'
    )


def refused(run: RunState, step: Any, message: str) -> None:
    """A start or a send refused under a service run. The setup caused it, not the agent: a
    library's own shell-out reads as a test mistake to fix, by listing it."""
    if step.decision.reason == "orphan":  # code outside the run's context: its own rule
        error = run.refusal(step)
        run.finish(step, error=error)
        raise error
    run.refuse(step, "service_child")
    step.extra["by_config"] = True
    error = ScrimmageRefused(message)
    run.finish(step, error=error, mistake=message)
    raise error


def refuse_other(run: RunState, label: str, what: str, why: str) -> None:
    """Something that would reach another process: refused under a run, never listed."""
    step = run.decide(Call("process", label, what), {"command": what})
    message = (
        f"{what} was used inside a test run, and {why}, so it is refused; no run of a service "
        "may reach another process. Do this work in the request's own process"
    )
    if step.decision.action == "real" and isinstance(run, Outside):
        run.finish(step)  # background_real lists it: work outside any request may
        return
    if step.decision.action == "real":  # listing it is not enough: it is never the run's
        step.decision = Decision("refused", reason="service_child")
    refused(run, step, message)


def guarded(what: str, why: str, method: Callable[..., Any], skip: type | None = None) -> Any:
    """method, refused under a service run before it does anything. skip: a class whose
    instances it lets through, such as a ThreadPool under Pool's methods."""
    if getattr(method, GATED, False):
        return method

    @functools.wraps(method)
    def gate(*args: Any, **kwargs: Any) -> Any:
        mine = bool(args) and (
            (skip is not None and isinstance(args[0], skip)) or args[0] in IN_PROCESS_QUEUES
        )
        if not in_service() or mine or getattr(_teardown, "on", False):
            return method(*args, **kwargs)
        run = current()
        # A run's own code or an orphan of one is refused. Work outside any request meets the
        # outside rules, except a pool's own, which carries what this gate decided already.
        if run is not None and not (isinstance(run, Outside) and pool_internal()):
            refuse_other(run, "process:multiprocessing", what, why)
        with internal_call():
            return method(*args, **kwargs)

    setattr(gate, GATED, True)
    return gate


def internal(method: Callable[..., Any]) -> Any:
    """A server's own method, named exactly, such as uvicorn's always_pong: while it runs, its
    sends are its own, outside any request."""
    if getattr(method, GATED, False):
        return method

    @functools.wraps(method)
    def marked(*args: Any, **kwargs: Any) -> Any:
        if not in_service():
            return method(*args, **kwargs)
        with internal_call():
            return method(*args, **kwargs)

    setattr(marked, GATED, True)
    return marked


_teardown = threading.local()  # set while a pool tears itself down: Pool.__del__, its finalizer


def teardown(method: Callable[..., Any]) -> Any:
    """A pool's teardown when it is dropped, Pool.__del__ and the finalizer's _terminate_pool:
    it sends its workers their stop and starts nothing, under any slot."""
    if getattr(method, GATED, False):
        return method

    @functools.wraps(method)
    def torn(*args: Any, **kwargs: Any) -> Any:
        was = getattr(_teardown, "on", False)
        _teardown.on = True
        try:
            return method(*args, **kwargs)
        finally:
            _teardown.on = was

    setattr(torn, GATED, True)
    return torn


def patch_names(
    module: ModuleType, owner: str, names: tuple[str, ...], why: str, skip: type | None = None
) -> None:
    cls = getattr(module, owner, None)
    for name in names:
        method = getattr(cls, name, None)
        if method is not None:
            setattr(cls, name, guarded(f"{owner}.{name}", why, method, skip))


SENT = "it would send this run's work to another process"


def install_service() -> None:
    """The per-library patches: where data leaves for another process, and the pools whose
    sends run on a thread of their own, after the work is recorded."""
    hooks.on_import(
        "multiprocessing.process",
        lambda m: patch_names(m, "BaseProcess", ("start",), "a new process would run ungated"),
    )
    hooks.on_import("subprocess", patch_popen)
    hooks.on_import("multiprocessing.resource_tracker", patch_tracker)
    if sys.version_info < (3, 14):  # from 3.14 its start raises an audit event of its own
        hooks.on_import("multiprocessing.util", patch_spawnv)
    hooks.on_import("multiprocessing.queues", patch_queues)
    hooks.on_import(
        "multiprocessing.connection",
        lambda m: patch_names(m, "_ConnectionBase", ("send", "send_bytes"), SENT),
    )
    hooks.on_import(
        "concurrent.futures.process",
        patch_executor,
    )
    hooks.on_import("multiprocessing.pool", patch_pool)
    # A uvicorn worker answers its supervisor's health pings over a pipe, on a thread of its
    # own: the server's traffic, never a request's.
    hooks.on_import("uvicorn.supervisors.multiprocess", patch_uvicorn_worker)
    for loky in ("loky.process_executor", "joblib.externals.loky.process_executor"):
        hooks.on_import(loky, patch_executor)
    hooks.on_import(
        "concurrent.futures.interpreter",
        patch_interpreter_pool,
    )
    hooks.on_import(
        "concurrent.interpreters",
        lambda m: patch_names(m, "Interpreter", ("exec", "call", "call_in_thread"), SUBINTERPRETER),
    )


def patch_tracker(module: ModuleType) -> None:
    """multiprocessing's resource tracker, which Python starts the first time a lock or a
    queue is made: a helper of its own that runs Python's code, never the team's, so its
    start is not a start of the run's. Python 3.14 starts it from _launch."""
    tracker: Any = getattr(module, "ResourceTracker", None)
    for name in ("ensure_running", "_launch"):
        method = getattr(tracker, name, None)
        if callable(method) and not getattr(method, GATED, False):
            setattr(tracker, name, tracker_start(method))
    shared: Any = getattr(module, "_resource_tracker", None)
    if isinstance(tracker, type) and isinstance(shared, tracker):
        setattr(module, "ensure_running", shared.ensure_running)  # noqa: B010  # bound at import


_tracker = threading.local()  # set while the resource tracker starts its helper
TRACKER_MAIN = "from multiprocessing.resource_tracker import main"


def tracker_start(method: Callable[..., Any]) -> Any:
    @functools.wraps(method)
    def start(*args: Any, **kwargs: Any) -> Any:
        was = getattr(_tracker, "on", False)
        _tracker.on = True
        try:
            return method(*args, **kwargs)
        finally:
            _tracker.on = was

    setattr(start, GATED, True)
    return start


def tracker(event: str, args: tuple[Any, ...]) -> bool:
    """This start is the resource tracker's own: made while it starts, running this Python,
    which multiprocessing's executable must be too, with the tracker's own code."""
    if not getattr(_tracker, "on", False) or event in ("os.fork", "os.forkpty", "os.system"):
        return False
    spawn = sys.modules.get("multiprocessing.spawn")
    chosen = getattr(spawn, "get_executable", lambda: None)()
    here = os.path.realpath(sys.executable)
    if chosen is None or os.path.realpath(os.fsdecode(chosen)) != here:
        return False
    if event == "_posixsubprocess.fork_exec":
        program = os.fsdecode(args[0][0]) if args and args[0] else ""
    else:
        program = os.fsdecode(command(event, args)[0])
    if os.path.realpath(program) != here:
        return False
    return any(arg.startswith(TRACKER_MAIN) for arg in argv_of(event, args))


def patch_spawnv(module: ModuleType) -> None:
    """multiprocessing.util.spawnv_passfds, which below Python 3.14 starts a program with no
    audit event: each start through it is decided as any other start is."""
    spawnv = getattr(module, "spawnv_passfds", None)
    if spawnv is None or getattr(spawnv, GATED, False):
        return

    @functools.wraps(spawnv)
    def decided(path: Any, args: Any, passfds: Any) -> Any:
        if in_service():
            argv = [os.fsdecode(a) for a in args]
            service_start("os.posix_spawn", (os.fsdecode(path), argv, {}))
        return spawnv(path, args, passfds)

    setattr(decided, GATED, True)
    setattr(module, "spawnv_passfds", decided)  # noqa: B010  # a module the SDK never imports


def patch_uvicorn_worker(module: ModuleType) -> None:
    worker = getattr(module, "Process", None)
    if not callable(getattr(worker, "always_pong", None)):
        process = state.process()
        if process is not None:
            process.structure(
                "uvicorn",
                "uvicorn's worker Process has no always_pong, so a worker's answers to its "
                "supervisor's health pings would be refused as sends outside any request, and "
                "every run would be an infrastructure error; the service is stopped",
                process.stop,
            )
        return
    mark_internal(module, "Process", ("always_pong",))


def patch_executor(module: ModuleType) -> None:
    """concurrent.futures' or loky's ProcessPoolExecutor."""
    patch_names(module, "ProcessPoolExecutor", ("submit",), SENT)
    mark_stops(module, "ProcessPoolExecutor", EXECUTOR_STOPS)
    manager = getattr(module, "_ExecutorManagerThread", None)  # it sends what submit decided
    if isinstance(manager, type) and manager not in state.INTERNAL_KINDS:
        state.INTERNAL_KINDS.append(manager)


def own_threads(module: ModuleType, owner: str, names: tuple[str, ...]) -> None:
    """A pool's or a queue's own thread targets, named exactly: a thread started with one of
    them as its target is the pool's own. No other thread is, whoever starts it."""
    cls = getattr(module, owner, None)
    for name in names:
        found: object = cls.__dict__.get(name) if isinstance(cls, type) else None
        function: object = getattr(found, "__func__", found)  # a staticmethod's function
        if callable(function):
            state.INTERNAL_TARGETS.add(function)


SUBINTERPRETER = "a subinterpreter has no Scrimmage in it, so its writes would run for real"


def patch_interpreter_pool(module: ModuleType) -> None:
    """InterpreterPoolExecutor: its submit is decided; its worker's run of a task, which a
    submit decided, is the pool's own call into its subinterpreter."""
    patch_names(module, "InterpreterPoolExecutor", ("submit",), SUBINTERPRETER)
    mark_internal(module, "WorkerContext", ("run",))


def patch_queues(module: ModuleType) -> None:
    patch_names(module, "Queue", ("put",), SENT)  # put_nowait calls put
    patch_names(module, "SimpleQueue", ("put",), SENT)
    own_threads(module, "Queue", ("_feed",))  # its feeder, which sends what put decided


def patch_pool(module: ModuleType) -> None:
    threads = getattr(module, "ThreadPool", None)
    names = ("apply_async", "_map_async", "imap", "imap_unordered")
    patch_names(module, "Pool", names, SENT, skip=threads)
    mark_stops(module, "Pool", POOL_STOPS, skip=threads)
    mark_teardown(module, "Pool", POOL_TEARDOWN)
    # Its handlers, which send what apply and map decided; the team's callbacks and items
    # they run are carried as the team's, in threadctx.
    own_threads(module, "Pool", ("_handle_workers", "_handle_tasks", "_handle_results"))
    pool: Any = getattr(module, "Pool", None)
    init = pool.__dict__.get("__init__") if isinstance(pool, type) else None
    if callable(init) and not getattr(init, GATED, False) and isinstance(threads, type):
        # A process pool's making is a decided start, as a Process.start is; then its own
        # starts and handler threads are the pool's. A ThreadPool's making starts no process.
        made = guarded("Pool", "a new process would run ungated", init, skip=threads)
        pool.__init__ = thread_pool_init(made, threads, getattr(module, "TERMINATE", "TERMINATE"))


def thread_pool_init(method: Callable[..., Any], threads: type, ended: object) -> Any:
    """Pool.__init__, which for a ThreadPool marks its change notifier, a pipe within this
    process, as no send to another one."""

    @functools.wraps(method)
    def init(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not in_service() or not isinstance(self, threads):
            try:
                return method(self, *args, **kwargs)
            except ScrimmageRefused:
                self._state = ended  # refused before it was made: its finalizer has nothing
                raise
        method(self, *args, **kwargs)
        notifier = getattr(self, "_change_notifier", None)
        for end in (
            notifier,
            getattr(notifier, "_writer", None),
            getattr(notifier, "_reader", None),
        ):
            if end is not None:  # the queue, and the pipe it sends through
                IN_PROCESS_QUEUES.add(end)
        return None

    setattr(init, GATED, True)
    return init


# A pool's teardown when it is dropped: it stops its workers and starts nothing.
POOL_TEARDOWN = ("__del__", "_terminate_pool")
# A pool's stops that send to its processes: decided at their start, so a run's refused stop
# leaves the pool as it was, never half closed.
POOL_STOPS = ("close", "terminate")
# The multiprocessing queues a ThreadPool uses within this process, such as its change notifier.
IN_PROCESS_QUEUES: "weakref.WeakSet[Any]" = weakref.WeakSet()
EXECUTOR_STOPS = ("shutdown",)


def mark_stops(
    module: ModuleType, owner: str, names: tuple[str, ...], skip: type | None = None
) -> None:
    cls = getattr(module, owner, None)
    for name in names:
        method = cls.__dict__.get(name) if isinstance(cls, type) else None
        if callable(method) and not getattr(method, GATED, False):
            setattr(cls, name, pool_stop(f"{owner}.{name}", method, skip))


def pool_stop(what: str, method: Callable[..., Any], skip: type | None) -> Any:
    """A pool's stop: refused under a run before it changes anything; outside any request,
    the pool's own work, as a lifespan's shutdown is."""

    @functools.wraps(method)
    def stop(*args: Any, **kwargs: Any) -> Any:
        if not in_service() or (skip is not None and args and isinstance(args[0], skip)):
            return method(*args, **kwargs)
        run = current()
        if run is not None and not isinstance(run, Outside):
            refuse_other(run, "process:multiprocessing", what, SENT)
        with internal_call():
            return method(*args, **kwargs)

    setattr(stop, GATED, True)
    return stop


def mark_internal(module: ModuleType, owner: str, names: tuple[str, ...]) -> None:
    cls = getattr(module, owner, None)
    for name in names:
        found: object = cls.__dict__.get(name) if isinstance(cls, type) else None
        if callable(found):
            setattr(cls, name, internal(found))


def mark_teardown(module: ModuleType, owner: str, names: tuple[str, ...]) -> None:
    cls = getattr(module, owner, None)
    for name in names:
        found: object = cls.__dict__.get(name) if isinstance(cls, type) else None
        if isinstance(found, classmethod):  # Pool._terminate_pool, which a finalizer calls
            function = cast(Callable[..., Any], found.__func__)  # pyright: ignore[reportUnknownMemberType]
            setattr(cls, name, classmethod(teardown(function)))
        elif callable(found):
            setattr(cls, name, teardown(found))


def refuse(run: RunState, python: str, argv: list[str], why: str) -> None:
    step = run.decide(Call("process", LABEL, python), {"command": shlex.join(argv)})
    if step.decision.action == "real":
        run.finish(step)
        return
    detail = (
        f"{python} would run with no gates, because {why}. Pass env={{**os.environ}}, and add to "
        f'PYTHONPATH rather than replace it, so it joins the run, or add "{LABEL}" to [agent] '
        "real if it is safe."
    )
    error = run.refusal(run.refuse(step, "blind_child"), detail)
    step.extra["by_config"] = True  # the test's setup caused this refusal, not the agent
    run.finish(step, error=error, mistake=str(error))
    raise error
