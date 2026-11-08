"""Agents that misbehave on purpose, for the runner's tests."""

import contextlib
import itertools
import os
import signal
import time
from pathlib import Path

from acme import tools


def mark(name: str) -> None:
    """Writes this process's pid to a file in one step, so a test can wait for the file."""
    Path(f"{name}.tmp").write_text(str(os.getpid()))
    os.replace(f"{name}.tmp", name)


def hang(question: str) -> str:
    mark("hanging")  # the test's event: the agent is in its sleep
    time.sleep(600)
    return "never"


def crash(question: str) -> str:
    raise RuntimeError("the bot crashed")


def stubborn(question: str) -> str:
    """Ignores SIGTERM, so only SIGKILL stops it."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    mark("ignoring")
    time.sleep(600)
    return "never"


def refuse_then_hang(question: str) -> str:
    """Swallows the refusal and keeps going, as a framework might."""
    with contextlib.suppress(Exception):
        tools.refund_order("1234")
    time.sleep(600)
    return "never"


def vanish(question: str) -> str:
    """Ends without Python's exit steps, so its last record batch is never sent."""
    tools.check_inventory("SKU-42")
    os._exit(0)


def echo(question: str) -> str:
    return question


def use_httpx2(question: str) -> str:
    """Brings in httpx2 by itself, for a test where the library has changed."""
    import httpx2

    return str(httpx2)


def claim(path: Path) -> bool:
    """True for the one run that makes this file, however many try at once."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        return False
    return True


def first_goes_quiet(question: str) -> str:
    """Runs take a role in turn. Nothing here depends on how fast a run starts.

    The first ends without reporting its tools. The second waits for the third to be
    going, then answers. The third waits to be stopped.
    """
    marks = Path("marks")
    marks.mkdir(exist_ok=True)
    role = next(n for n in itertools.count(1) if claim(marks / str(n)))
    if role == 1:
        os._exit(0)
    if role == 2:
        while not (marks / "3").exists():
            time.sleep(0.01)
        return "done"
    time.sleep(600)
    return "never"
