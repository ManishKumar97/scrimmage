"""The starter for a function agent: python -m faultline_sdk.start module:function question.

It calls the function with the question, waits for it if it is async, and writes the
answer to FAULTLINE_ANSWER_FILE, so the agent's own prints cannot mix with it.
"""

import asyncio
import importlib
import inspect
import json
import os
import sys
from collections.abc import Awaitable


async def _wait(value: Awaitable[object]) -> object:
    return await value


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python -m faultline_sdk.start module:function question",
            file=sys.stderr,
        )
        return 2
    module, _, name = argv[1].partition(":")
    function = getattr(importlib.import_module(module), name)
    result = function(argv[2])
    if inspect.isawaitable(result):
        result = asyncio.run(_wait(result))
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
