"""The starter for a function agent: python -m faultline_sdk.start module:function question.

It calls the function with the question, waits for it if it is async, and writes the
answer to FAULTLINE_ANSWER_FILE, so the agent's own prints cannot mix with it.
"""

import asyncio
import importlib
import inspect
import json
import os
import re
import sys
from collections.abc import Awaitable, Mapping, Sequence
from typing import cast


async def _wait(value: Awaitable[object]) -> object:
    return await value


STEP = re.compile(r"\[(-?\d+)\]|\.?([A-Za-z_]\w*)")


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
        except (LookupError, TypeError, AttributeError) as exc:
            raise SystemExit(
                f"faultline: [agent] answer {path!r} does not fit what the agent returned: "
                f"{step.group()!r} failed with {exc!r}"
            ) from None
    return value


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: python -m faultline_sdk.start module:function question [answer path]",
            file=sys.stderr,
        )
        return 2
    module, _, name = argv[1].partition(":")
    function = getattr(importlib.import_module(module), name)
    result = function(argv[2])
    if inspect.isawaitable(result):
        result = asyncio.run(_wait(result))
    if len(argv) == 4:
        result = pick(result, argv[3])
    answer = result if isinstance(result, str) else json.dumps(result, default=str)
    path = os.environ.get("FAULTLINE_ANSWER_FILE")
    if path:
        with open(path, "w") as out:
            out.write(answer)
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
