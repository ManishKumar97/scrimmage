"""A guard the agent waits on with a deadline of its own, and the fallback it takes when late.

This is how a guard usually fails open or closed in production: not by raising, but by
answering after the caller has stopped waiting.
"""

import asyncio

DEADLINE_S = 0.5


async def check_answer(text: str) -> str:
    """The guard: says whether an answer may go out."""
    return "allow"


def answer(question: str) -> str:
    async def go() -> str:
        try:
            verdict = await asyncio.wait_for(check_answer(question), timeout=DEADLINE_S)
        except TimeoutError:
            return "the guard was late, so the fallback: blocked"
        return f"the guard said {verdict}: allowed"

    return asyncio.run(go())
