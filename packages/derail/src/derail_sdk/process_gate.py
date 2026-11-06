"""The gate for child processes, on Python's audit events.

A child on the run's Python that does not inherit Derail's environment, or that is told to
skip the startup file, would run its writes with no gate and leave no line. Python reports
every process start to an audit hook, which can refuse it. Only starts made from Python are
seen: sh -c 'python x', env -i python and an exec from another program are not.
"""

import os
import re
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from derail_sdk.decide import Call
from derail_sdk.labels import PROCESS as LABEL
from derail_sdk.state import RunState

BOOT = str(Path(__file__).parent / "_boot")
EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn")
PYTHON = re.compile(r"(?:python|pypy)[\d.]*")
SKIPS = set("IES")  # python flags that ignore PYTHONPATH or skip the startup file
# The MCP gate decides a stdio server's calls, so its own start of the server is not blind.
EXEMPT: ContextVar[bool] = ContextVar("derail_process_exempt", default=False)


def command(event: str, args: tuple[Any, ...]) -> tuple[str, list[str], str, dict[str, str]]:
    """The program, its arguments, its folder and its environment, for any of the events."""
    if event == "subprocess.Popen":
        program, argv, folder, env = args
    else:
        program, argv, env = args[-3:]
        folder = None
    named = cast(Mapping[Any, Any], os.environ if env is None else env)
    return (
        os.fsdecode(program),
        [os.fsdecode(arg) for arg in argv],
        "" if folder is None else os.fsdecode(folder),
        {os.fsdecode(key): os.fsdecode(value) for key, value in named.items()},
    )


def interpreter(path: str, env: dict[str, str]) -> tuple[str, list[str]] | None:
    """The Python a file runs and the flags its shebang gives, or None if it is not Python."""
    if PYTHON.fullmatch(os.path.basename(path)):
        return path, []
    try:
        with open(path, "rb") as file:
            line = file.readline(256)
    except OSError:
        return None
    words = line[2:].decode(errors="replace").split() if line.startswith(b"#!") else []
    for index, word in enumerate(words):
        if PYTHON.fullmatch(os.path.basename(word)):
            return shutil.which(word, path=env.get("PATH")) or word, words[index + 1 :]
    return None


def is_this_python(path: str) -> bool:
    """Another Python has its own packages, and runs untouched even with the environment."""
    bin_folder = os.path.join(sys.prefix, "bin")
    same_binary = os.path.realpath(path) == os.path.realpath(sys.executable)
    return same_binary or os.path.dirname(os.path.abspath(path)) == bin_folder


def flags(args: list[str]) -> set[str]:
    """The single-letter flags before the program Python is told to run."""
    found: set[str] = set()
    words = iter(args)
    for word in words:
        if not word.startswith("-") or word in ("-", "--"):
            break
        for letter in "" if word.startswith("--") else word[1:]:
            if letter in "cm":
                return found
            if letter in "WX":
                if word.endswith(letter):
                    next(words, None)
                break
            found.add(letter)
    return found


def why_blind(env: dict[str, str], args: list[str]) -> str | None:
    if "DERAIL_TOKEN" not in env or BOOT not in env.get("PYTHONPATH", "").split(os.pathsep):
        return "its environment does not hold Derail's variables"
    skipped = sorted(flags(args) & SKIPS)
    return f"-{skipped[0]} skips Derail's startup file" if skipped else None


def install(run: RunState, before_exec: Callable[[], None]) -> None:
    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event not in EVENTS:
            return
        program, argv, folder, env = command(event, args)
        found = program if "/" in program else shutil.which(program, path=env.get("PATH"))
        target = interpreter(os.path.join(folder, found), env) if found else None
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
        f"{python} would run with no gates, because {why}. Pass env={{**os.environ}} so it joins "
        f'the run, or add "{LABEL}" to [agent] real if it is safe.'
    )
    error = run.refusal(run.refuse(step, "blind_child"), detail)
    run.finish(step, error=error, mistake=str(error))
    raise error
