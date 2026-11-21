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

import contextlib
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

from scrimmage import hooks, python_args, state
from scrimmage.decide import Call, Decision
from scrimmage.errors import ScrimmageRefused
from scrimmage.labels import PROCESS as LABEL
from scrimmage.labels import PROGRAM
from scrimmage.state import (
    Outside,
    RunState,
    carried,
    current,
    in_service,
    internal_call,
    pool_internal,
)

BOOT = os.path.join("scrimmage", "_boot")  # the runner's, which may be in another venv
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
    if run is None or (isinstance(run, Outside) and pool_internal()):
        return
    program, line = program_of(event, args)
    if program is None:
        refuse_other(run, "process:fork", "os.fork", "a fork would carry this run into a copy")
        return
    if not PROGRAM.fullmatch(program):  # an empty name, or one with a line break or odd bytes
        call = Call("process", "process:?", line)
        where = (
            "outside any request while a test run was open"
            if isinstance(run, Outside)
            else ("inside a test run")
        )
        message = (
            f"{line} was started {where}, and its program has no plain name that "
            "[agent] real could list, so it is refused. Start it by a name such as uname, "
            "through a link or wrapper whose name real can list."
        )
        if isinstance(run, Outside):  # no entry could let it through, so no entry is named
            for open_run in run.runs:
                open_run.outside_refused(call, message, None, {"command": line})
            raise ScrimmageRefused(message)
        refused(run, run.decide(call, {"command": line}), message)
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
        run.fail(step, run.refusal(step))
    run.refuse(step, "service_child")
    step.extra["by_config"] = True
    run.fail(step, ScrimmageRefused(message), mistake=True)


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
        if not in_service() or (skip is not None and args and isinstance(args[0], skip)):
            return method(*args, **kwargs)
        if args and (own_object(args[0]) or fed_send(args)):
            return method(*args, **kwargs)
        decide_send(what, why)
        with internal_call():
            return method(*args, **kwargs)

    setattr(gate, GATED, True)
    return gate


def decide_send(what: str, why: str) -> None:
    """A send or a start to another process, decided where the code is: refused under a run
    or an orphan of one; outside any request, the outside rules, unless a pool's own call
    or thread already carries a decision."""
    run = current()
    if run is not None and not (isinstance(run, Outside) and pool_internal()):
        refuse_other(run, "process:multiprocessing", what, why)


def own_object(target: object) -> bool:
    """An object Scrimmage registered, by identity: a pipe within this process, such as a
    ThreadPool's change notifier, at any time; while a pool tears itself down, that pool's
    own queues and pipes, and no other pool's."""
    try:
        if target in IN_PROCESS_QUEUES:
            return True
    except TypeError:  # an object that cannot be weakly held is no registered one
        return False
    torn: object = getattr(_teardown, "ends", ())
    return any(target is end for end in cast(tuple[object, ...], torn))


# On a feeder thread: the pipe and the exact pickled bytes of the decided item it just
# pickled, which its next send may carry, once.
_fed = threading.local()


def fed_send(args: tuple[Any, ...]) -> bool:
    """A queue's own feeder thread, sending on its own pipe the very bytes its check let
    through. Any gated call on the thread spends the record, so nothing made while the item
    was pickled, nor after a failed pickle, can use it."""
    sent: tuple[object, object] | None = getattr(_fed, "sent", None)
    _fed.sent = None
    if sent is None or len(args) < 2:
        return False
    pipe, pickled = sent
    try:
        own = state.FEEDERS.get(threading.current_thread()) is pipe
    except TypeError:
        return False
    return own and args[0] is pipe and args[1] is pickled


def fed_pickle(item: object, pickle: Callable[[], Any]) -> Any:
    """A feeder's pickling of item: checked first, and its bytes recorded for the send only
    once the pickling returned. A pickling inside it, such as a reducer's, is checked as
    part of that item, and records nothing."""
    _fed.sent = None
    if getattr(_fed, "depth", 0):
        return pickle()
    pipe = feeder_item(item)
    _fed.depth = 1
    try:
        pickled = pickle()
    finally:
        _fed.depth = 0
    if pipe is not None:
        _fed.sent = (pipe, pickled)
    return pickled


UNFED = (
    "an item was added to a multiprocessing queue's buffer without the queue's put, so its "
    "feeder refused to send it to another process; only an item a put decided is sent"
)


def feeder_item(item: object) -> object:
    """A feeder thread is about to pickle and send item: it may, once, only if a decided put
    added it to the buffer this feeder reads. Any other item is refused, and every open run
    is told, as any of them could have put it there."""
    thread = threading.current_thread()
    buffer = state.FEED_BUFFERS.get(thread)
    if buffer is None or not in_service():
        return None  # not a feeder: its sends are decided as any other send
    if state.take_decided(buffer, item):
        return state.FEEDERS.get(thread)  # the pipe a registered feeder may send it on
    process = state.process()
    runs = process.open_runs() if process is not None else []
    call = Call("process", "process:multiprocessing", "Queue._feed")
    for run in runs:
        run.outside_refused(call, UNFED, None)
    if not runs:  # no record to write to: the service's log says so
        print(f"scrimmage: {UNFED}", file=sys.stderr, flush=True)
    raise ScrimmageRefused(UNFED)


def checked_pickler(base: type) -> type:
    """multiprocessing.queues' ForkingPickler, whose dumps first checks a feeder's item."""

    class ForkingPickler(base):  # type: ignore[misc, valid-type]
        @classmethod
        def dumps(cls, obj: Any, protocol: Any = None) -> Any:
            pickled: Callable[..., Any] = getattr(base, "dumps")  # noqa: B009  # base pickles it
            return fed_pickle(obj, lambda: pickled(obj, protocol))

    ForkingPickler.__qualname__ = base.__qualname__
    ForkingPickler.__module__ = base.__module__
    setattr(ForkingPickler, GATED, True)
    return ForkingPickler


def checked_dumps(dumps: Callable[..., Any]) -> Any:
    """loky's queue dumps, which first checks a feeder's item."""

    @functools.wraps(dumps)
    def checked(obj: Any, *args: Any, **kwargs: Any) -> Any:
        return fed_pickle(obj, lambda: dumps(obj, *args, **kwargs))

    setattr(checked, GATED, True)
    return checked


def recorded(put: Callable[..., Any]) -> Any:
    """A queue's own put, run once the gate decided it: the item it adds to the buffer its
    feeder reads is noted by identity, so the feeder sends that item, and no other."""

    @functools.wraps(put)
    def noted(self: Any, *args: Any, **kwargs: Any) -> Any:
        buffer = getattr(self, "_buffer", None) if in_service() else None
        if buffer is None or not (args or "obj" in kwargs):
            return put(self, *args, **kwargs)
        item = args[0] if args else kwargs["obj"]
        state.note_decided(buffer, item)
        try:
            return put(self, *args, **kwargs)
        except BaseException:
            state.drop_decided(buffer, item)
            raise

    return noted


def patch_feeder(module: ModuleType) -> None:
    """multiprocessing's Queue, or loky's: its own _feed is a feeder target. A thread started
    inside a decided put with that exact target is that queue's feeder, and its sends on
    that queue's pipe, and only those, carry what the put decided."""
    queue: Any = getattr(module, "Queue", None)
    found: object = queue.__dict__.get("_feed") if isinstance(queue, type) else None
    feed: object = getattr(found, "__func__", found)  # a staticmethod's function
    if callable(feed):
        state.FEEDER_TARGETS.add(feed)
    start = queue.__dict__.get("_start_thread") if isinstance(queue, type) else None
    if callable(start) and not getattr(start, GATED, False):
        queue._start_thread = starting(start)


def starting(method: Callable[..., Any]) -> Any:
    """Queue._start_thread: the one place a queue's own feeder is started, so the only
    place a feeder is registered, and only as this queue's."""

    @functools.wraps(method)
    def start(self: Any, *args: Any, **kwargs: Any) -> Any:
        with state.starting_feeder(self):
            return method(self, *args, **kwargs)

    setattr(start, GATED, True)
    return start


def internal(method: Callable[..., Any]) -> Any:
    """A server's own method, named exactly, such as uvicorn's always_pong: while it runs, its
    sends are its own, outside any request."""
    if getattr(method, GATED, False):
        return method

    @functools.wraps(method)
    def marked(*args: Any, **kwargs: Any) -> Any:
        if not in_service():
            return method(*args, **kwargs)
        run = current()
        if run is not None and not isinstance(run, Outside):
            return method(*args, **kwargs)  # called by a run's code: the run's own, decided
        with internal_call():
            return method(*args, **kwargs)

    setattr(marked, GATED, True)
    return marked


_teardown = threading.local()  # set while a pool tears itself down: Pool.__del__, its finalizer


def pool_ends(given: object) -> list[object]:
    """The ends recorded for a pool or a queue; a queue's own entry names itself weakly."""
    ends: list[object] = []
    for end in POOL_ENDS.get(given, ()):
        found = cast("object | None", end() if isinstance(end, weakref.ref) else end)
        if found is not None:
            ends.append(found)
    return ends


def teardown(method: Callable[..., Any]) -> Any:
    """A pool's teardown when it is dropped, Pool.__del__ and the finalizer's _terminate_pool:
    it sends its workers their stop and starts nothing, under any slot."""
    if getattr(method, GATED, False):
        return method

    @functools.wraps(method)
    def torn(*args: Any, **kwargs: Any) -> Any:
        # The ends of the pool or the queues this teardown was given, and nothing else.
        mine: list[object] = []
        for given in [*args, *kwargs.values()]:
            with contextlib.suppress(TypeError):
                mine += pool_ends(given)
        was = getattr(_teardown, "ends", ())
        _teardown.ends = tuple(mine)
        try:
            return method(*args, **kwargs)
        finally:
            _teardown.ends = was

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
    hooks.on_import(  # a queue a subinterpreter reads: its put hands work to code with no gate
        "concurrent.interpreters._queues",
        lambda m: patch_names(m, "Queue", ("put", "put_nowait"), SUBINTERPRETER),
    )
    for loky in ("loky.backend.queues", "joblib.externals.loky.backend.queues"):
        hooks.on_import(loky, patch_queues)  # loky's own queues, with a feeder of their own


def start_tracker() -> None:
    """Python's resource tracker, which multiprocessing starts the first time a lock, a queue
    or shared memory is made, started where the app is served, as it is loaded, or where
    uvicorn supervises workers, before it starts one; both come before any request. No run
    then needs to start it, and a process that never serves starts none. A start of it later,
    a relaunch included, is decided as any other start is: refused under a run, and under the
    outside rules unless background_real lists it. A worker multiprocessing started gets its
    parent's tracker instead."""
    if "spawn_main" in " ".join(getattr(sys, "orig_argv", [])):
        return
    from multiprocessing import resource_tracker

    resource_tracker.ensure_running()


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
    supervisor: Any = getattr(module, "Multiprocess", None)
    run = supervisor.__dict__.get("run") if isinstance(supervisor, type) else None
    if callable(run) and not getattr(run, GATED, False):
        supervisor.run = tracked(run)


def tracked(run: Callable[..., Any]) -> Any:
    """uvicorn's Multiprocess.run: the supervisor starts the tracker before its workers,
    which then share it."""

    @functools.wraps(run)
    def supervise(self: Any, *args: Any, **kwargs: Any) -> Any:
        if in_service():
            start_tracker()
        return run(self, *args, **kwargs)

    setattr(supervise, GATED, True)
    return supervise


def patch_executor(module: ModuleType) -> None:
    """concurrent.futures' or loky's ProcessPoolExecutor."""
    patch_names(module, "ProcessPoolExecutor", ("submit",), SENT)
    mark_stops(module, "ProcessPoolExecutor", EXECUTOR_STOPS)
    manager = getattr(module, "_ExecutorManagerThread", None)  # it sends what submit decided
    if isinstance(manager, type) and manager not in state.INTERNAL_KINDS:
        state.INTERNAL_KINDS.append(manager)
    wakeup: Any = getattr(module, "_ThreadWakeup", None)
    init = wakeup.__dict__.get("__init__") if isinstance(wakeup, type) else None
    if callable(init) and not getattr(init, GATED, False):
        wakeup.__init__ = in_process_pipe(init)


def in_process_pipe(init: Callable[..., Any]) -> Any:
    """An executor's wakeup pipe, made by it: both its ends stay in this process, one with the
    caller and one with the manager thread, so a send on it reaches no other process."""

    @functools.wraps(init)
    def made(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        for end in (getattr(self, "_writer", None), getattr(self, "_reader", None)):
            if end is not None:
                IN_PROCESS_QUEUES.add(end)

    setattr(made, GATED, True)
    return made


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
    """Every queue class of the module whose own put sends, now or through its feeder; a
    subclass that defines put of its own, such as JoinableQueue, is decided too."""
    for name, cls in sorted(vars(module).items()):
        if isinstance(cls, type) and "put" in cls.__dict__ and name.endswith("Queue"):
            put = cls.__dict__["put"]
            if not getattr(put, GATED, False):
                setattr(cls, "put", recorded(put))  # noqa: B010  # inside the gate below
            patch_names(module, name, ("put",), SENT)  # put_nowait calls put
    patch_feeder(module)
    # What a feeder pickles is checked first: mp's queues pickle with _ForkingPickler,
    # loky's with dumps.
    pickler: object = getattr(module, "_ForkingPickler", None)
    if isinstance(pickler, type) and not getattr(pickler, GATED, False):
        setattr(module, "_ForkingPickler", checked_pickler(pickler))  # noqa: B010
    dumps: object = module.__dict__.get("dumps")
    if callable(dumps) and not getattr(dumps, GATED, False):
        setattr(module, "dumps", checked_dumps(dumps))  # noqa: B010
    simple: Any = getattr(module, "SimpleQueue", None)
    init = simple.__dict__.get("__init__") if isinstance(simple, type) else None
    if callable(init) and not getattr(init, GATED, False):
        simple.__init__ = seen_made(init)


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
        if not in_service():
            return method(self, *args, **kwargs)
        was = getattr(_making, "queues", None)
        # Every SimpleQueue made while this pool is made, with the ends its own init made.
        made: list[tuple[object, object, object]] = []
        _making.queues = made
        try:
            method(self, *args, **kwargs)
        except ScrimmageRefused:
            self._state = ended  # refused before it was made: its finalizer has nothing
            raise
        finally:
            _making.queues = was
        # Its own queues and pipes, by identity, only if they were made here, not handed in by
        # a context of the team's: its teardown may send on these, and no other pool's.
        ends: list[object] = []
        for name in ("_inqueue", "_outqueue", "_change_notifier"):
            queue = getattr(self, name, None)
            found = [(r, w) for q, r, w in made if q is queue]
            if not found:
                continue
            reader, writer = found[0]
            # Only the ends its init made, and only while they are still its ends: a context
            # of the team's may have swapped in another queue's pipe.
            if getattr(queue, "_reader", None) is not reader:
                continue
            if getattr(queue, "_writer", None) is not writer:
                continue
            mine = [e for e in (queue, writer, reader) if e is not None]
            ends += mine
            with contextlib.suppress(TypeError):
                # The queue is the key, so its own entry holds it only weakly: a value that
                # holds its key keeps it, and its two pipe ends, open for good.
                POOL_ENDS[queue] = (weakref.ref(queue), *(e for e in mine if e is not queue))
                if isinstance(self, threads) and name == "_change_notifier":
                    for end in mine:
                        IN_PROCESS_QUEUES.add(end)  # a pipe within this process
        with contextlib.suppress(TypeError):
            POOL_ENDS[self] = tuple(ends)
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
# A pool, and each queue it made as it was made, with the queues and pipes that pool may send
# on as it tears itself down.
POOL_ENDS: "weakref.WeakKeyDictionary[Any, tuple[object, ...]]" = weakref.WeakKeyDictionary()
_making = threading.local()  # the SimpleQueues made while a pool is being made


def seen_made(init: Callable[..., Any]) -> Any:
    """multiprocessing's SimpleQueue.__init__: one made while a pool is made is noted, so only
    a queue the pool itself made is ever its own."""

    @functools.wraps(init)
    def noted(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        making = getattr(_making, "queues", None)
        if making is not None:  # the ends as its own init made them
            making.append((self, getattr(self, "_reader", None), getattr(self, "_writer", None)))

    setattr(noted, GATED, True)
    return noted


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
    refused_step = run.refuse(step, "blind_child")
    step.extra["by_config"] = True  # the test's setup caused this refusal, not the agent
    run.fail(step, run.refusal(refused_step, detail), mistake=True)
