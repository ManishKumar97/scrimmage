"""The gate for child processes, on Python's audit events.

A child on the run's Python that does not inherit Scrimmage's environment, or that is told to
skip the startup file, would run its writes with no gate and leave no line. Python reports
every process start to an audit hook, which can refuse it. Only starts made from Python are
seen: sh -c 'python x', env -i python and an exec from another program are not.

In a service, a run is one request in a process that serves others, so nothing it does may
reach another process: every start under a run is refused unless real lists its program, and
so are a fork, a multiprocessing process, every multiprocessing send, and the process pools
whose sends run on threads of their own. Outside any run, all of it is untouched.
"""

import functools
import os
import shlex
import shutil
import sys
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from types import ModuleType
from typing import Any, cast

from scrimmage_core import hooks, python_args
from scrimmage_core.decide import Call, Decision
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.labels import PROCESS as LABEL
from scrimmage_core.state import Outside, RunState, carried, current, in_service

BOOT = os.path.join("scrimmage_core", "_boot")  # the runner's, which may be in another venv
EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn")  # os.spawnv is exec here
# In a service every start is watched, a fork and os.system too. On 3.14 a Popen raises
# _posixsubprocess.fork_exec as well, the same start, decided once.
SERVICE_EVENTS = (*EVENTS, "os.fork", "os.forkpty", "os.system", "_posixsubprocess.fork_exec")
GATED = "__scrimmage_gate__"
_popen = threading.local()  # a Popen this thread just let through: its fork_exec is the same
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
    if event == "_posixsubprocess.fork_exec" and getattr(_popen, "on", False):
        _popen.on = False  # the start its Popen event decided already
        return
    run = current()
    if run is None or EXEMPT.get():
        return
    program, line = program_of(event, args)
    if program is None:
        refuse_other(run, "process:fork", "os.fork", "a fork would carry this run into a copy")
        return
    label = f"process:{program}"
    step = run.decide(Call("process", label, program), {"command": line})
    if step.decision.action == "real":
        run.finish(step)
        _popen.on = event == "subprocess.Popen"
        return
    refused(run, step, program_fix(program))


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
        if in_service() and not (skip is not None and args and isinstance(args[0], skip)):
            run = current()
            # A run's own code, or an orphan of one. A pool's own threads run outside any
            # request, and their sends are a plain request's, never a run's.
            if run is not None and not isinstance(run, Outside):
                refuse_other(run, "process:multiprocessing", what, why)
        return method(*args, **kwargs)

    setattr(gate, GATED, True)
    return gate


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
    hooks.on_import("multiprocessing.queues", patch_queues)
    hooks.on_import(
        "multiprocessing.connection",
        lambda m: patch_names(m, "_ConnectionBase", ("send", "send_bytes"), SENT),
    )
    hooks.on_import(
        "concurrent.futures.process",
        lambda m: patch_names(m, "ProcessPoolExecutor", ("submit",), SENT),
    )
    hooks.on_import("multiprocessing.pool", patch_pool)
    for loky in ("loky.process_executor", "joblib.externals.loky.process_executor"):
        hooks.on_import(loky, lambda m: patch_names(m, "ProcessPoolExecutor", ("submit",), SENT))
    hooks.on_import(
        "concurrent.futures.interpreter",
        lambda m: patch_names(m, "InterpreterPoolExecutor", ("submit",), SUBINTERPRETER),
    )
    hooks.on_import(
        "concurrent.interpreters",
        lambda m: patch_names(m, "Interpreter", ("exec", "call", "call_in_thread"), SUBINTERPRETER),
    )


SUBINTERPRETER = "a subinterpreter has no Scrimmage in it, so its writes would run for real"


def patch_queues(module: ModuleType) -> None:
    patch_names(module, "Queue", ("put",), SENT)  # put_nowait calls put
    patch_names(module, "SimpleQueue", ("put",), SENT)


def patch_pool(module: ModuleType) -> None:
    threads = getattr(module, "ThreadPool", None)
    names = ("apply_async", "_map_async", "imap", "imap_unordered")
    patch_names(module, "Pool", names, SENT, skip=threads)


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
