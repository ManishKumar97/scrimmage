"""The gate for child processes, on Python's audit events.

A child on the run's Python that does not inherit Scrimmage's environment, or that is told to
skip the startup file, would run its writes with no gate and leave no line. Python reports
every process start to an audit hook, which can refuse it. Only starts made from Python are
seen: sh -c 'python x', env -i python and an exec from another program are not.
"""

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any, cast

from scrimmage_core import python_args
from scrimmage_core.decide import Call
from scrimmage_core.labels import PROCESS as LABEL
from scrimmage_core.state import RunState

BOOT = os.path.join("scrimmage_core", "_boot")  # the runner's, which may be in another venv
EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn")  # os.spawnv is exec here
# The MCP gate decides a stdio server's calls, so its own start of the server is not blind.
EXEMPT: ContextVar[bool] = ContextVar("scrimmage_process_exempt", default=False)


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


def install(run: RunState, before_exec: Callable[[], None]) -> None:
    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event not in EVENTS:
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

    sys.addaudithook(hook)


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
    run.finish(step, error=error, mistake=str(error))
    raise error
