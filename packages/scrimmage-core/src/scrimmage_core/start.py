"""The starter for a function agent: python -m scrimmage_core.start module:function question.

It calls the function with the question, waits for it if it is async, and writes the
answer to SCRIMMAGE_ANSWER_FILE, so the agent's own prints cannot mix with it.
"""

import asyncio
import importlib
import inspect
import os
import re
import sys
from collections.abc import Awaitable, Mapping, Sequence
from typing import cast

from scrimmage_core.state import current


async def _wait(value: Awaitable[object]) -> object:
    return await value


class BadAnswerPath(Exception):
    """The path does not fit what the agent returned. A mistake in the test."""


STEP = re.compile(r"\[(-?\d+)\]|\.?([A-Za-z_]\w*)")
NOT_AN_ANSWER = (
    "[agent] answer: {path!r} is a {kind}, not the answer, so all of it would be judged, tool "
    "results included. Point answer at the text inside it; see docs/frameworks.md"
)
NOT_TEXT = (
    "[agent] answer: the agent returned {kind}, not text, so the whole value would be judged, "
    "tool results included. Set answer to the path of the final reply, such as "
    "'messages[-1].text'; see docs/frameworks.md"
)


def pick(value: object, path: str) -> object:
    """The part of a returned value named by a path, such as messages[-1].content."""
    for step in STEP.finditer(path):
        index, name = step.groups()
        try:
            if index is not None:
                value = cast(Sequence[object], value)[int(index)]
            elif isinstance(value, Mapping):
                value = cast(Mapping[str, object], value)[name]
            else:
                value = getattr(value, name)
        except Exception as exc:  # a property on the way can raise anything
            raise BadAnswerPath(
                f"[agent] answer {path!r} does not fit what the agent returned: "
                f"{step.group()!r} failed with {exc!r}"
            ) from None
    # LangChain's .text is a str that can also be called, as an older spelling of it.
    if value is None or (callable(value) and not isinstance(value, str)):
        raise BadAnswerPath(f"[agent] answer {path!r} reaches {value!r}, which is not an answer")
    return value


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: python -m scrimmage_core.start module:function question [answer path]",
            file=sys.stderr,
        )
        return 2
    module, _, name = argv[1].partition(":")
    function = getattr(importlib.import_module(module), name)
    result = function(argv[2])
    if inspect.isawaitable(result):
        result = asyncio.run(_wait(result))
    try:
        if len(argv) == 4:
            result = pick(result, argv[3])
            if not isinstance(result, str | int | float):
                raise BadAnswerPath(NOT_AN_ANSWER.format(path=argv[3], kind=type(result).__name__))
        elif not isinstance(result, str):
            raise BadAnswerPath(NOT_TEXT.format(kind=type(result).__name__))
    except BadAnswerPath as exc:
        run = current()
        if run is not None:
            run.notice("answer_path", "scrimmage", str(exc), mistake=str(exc))
        print(f"scrimmage: {exc}", file=sys.stderr)
        return 1
    # str() of a text enum is its name, not its value.
    answer = result if isinstance(result, str) else str(result)
    path = os.environ.get("SCRIMMAGE_ANSWER_FILE")
    if path:
        with open(path, "w", encoding="utf-8", errors="replace") as out:
            out.write(answer)
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
