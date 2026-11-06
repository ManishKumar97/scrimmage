"""Agents that misbehave on purpose, for the runner's tests."""

import contextlib
import os
import signal
import time
from pathlib import Path

from acme import tools


def hang(question: str) -> str:
    time.sleep(600)
    return "never"


def crash(question: str) -> str:
    raise RuntimeError("the bot crashed")


def stubborn(question: str) -> str:
    """Ignores SIGTERM, so only SIGKILL stops it."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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


def first_goes_quiet(question: str) -> str:
    """The first run hangs, so it reports nothing; the second ends; the third hangs."""
    marks = Path("marks")
    marks.mkdir(exist_ok=True)
    (marks / str(os.getpid())).touch()
    if len(list(marks.iterdir())) != 2:
        time.sleep(600)
    return "done"
