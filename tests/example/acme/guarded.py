"""Acme's shop bot behind its own guardrails: a guard model, and a check on the answer.

The guard model is a Llama Guard served on guard.test, asked through the openai library under
a deadline of its own. Its reply starts `safe` or `unsafe`. The bot's policy for a guard that
fails, is late or says something else is the question's first word: `open:` goes on
unchecked, `closed:` refuses. With no such word it is closed.

Each entry point below is the same bot with its guards placed differently, as teams do.
"""

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2
import openai

from acme import agent, guards

GUARD_MODEL = "llama-guard-test"
GUARD_URL = "http://guard.test/v1"
DEADLINE_S = 2.0  # the bot stops waiting for its guard after this
BLOCKED = "Request blocked by policy RB7"  # the guard's own message, unique to it
LATER = "The safety check is down, please try again later"
SPECS = [s for s in agent.SPECS if s["function"]["name"] in ("get_order", "refund_order")]


class Unavailable(Exception):
    """A guard failed, was late or said something else, under the closed policy."""


class Caught(Exception):
    """A guard said no."""


class Forward(httpx2.AsyncBaseTransport):
    """Sends every request to one local address, as a test's DNS entry for guard.test would.
    The request keeps its own URL, so whatever reads it above the transport sees guard.test."""

    def __init__(self, address: str) -> None:
        self._host, port = address.rsplit(":", 1)
        self._port = int(port)
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        request.url = request.url.copy_with(scheme="http", host=self._host, port=self._port)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def guard_client() -> openai.AsyncOpenAI:
    at = os.environ.get("ACME_GUARD_AT")  # set by tests; in production DNS finds guard.test
    http = openai.DefaultAsyncHttpxClient(transport=Forward(at)) if at else None
    return openai.AsyncOpenAI(
        base_url=GUARD_URL,
        api_key=os.environ.get("ACME_GUARD_KEY", "none"),
        max_retries=0,
        http_client=http,
    )


async def ask_guard(text: str) -> str:
    async with guard_client() as client:
        reply = await client.chat.completions.create(
            model=GUARD_MODEL, messages=[{"role": "user", "content": text}]
        )
    return reply.choices[0].message.content or ""


def guard_allows(text: str) -> bool:
    """The guard model's verdict, within the deadline. Anything but `safe` or `unsafe` on its
    first line is a guard that failed, and raises."""
    verdict = asyncio.run(asyncio.wait_for(ask_guard(text), DEADLINE_S))
    first = verdict.strip().splitlines()[0] if verdict.strip() else ""
    if first in ("safe", "unsafe"):
        return first == "safe"
    raise ValueError(f"the guard said {verdict!r}")


def split(question: str) -> tuple[bool, str]:
    """(fails open, the question without its policy word)."""
    word, _, rest = question.partition(":")
    if word in ("open", "closed"):
        return word == "open", rest.strip()
    return False, question


def checked(allows: Callable[[str], bool], text: str, fails_open: bool) -> None:
    """Raise Caught when the guard says no, and Unavailable when it fails under closed."""
    try:
        ok = allows(text)
    except Exception as exc:
        if fails_open:
            return
        raise Unavailable from exc
    if not ok:
        raise Caught


def answer_allows(text: str) -> bool:
    return guards.check_answer(text) == "allow"


def run(
    question: str,
    *,
    tools_read: Callable[[str], str] | None = None,
    guard_tools: bool = False,
    output: Callable[[str], bool] | None = answer_allows,
) -> str:
    fails_open, question = split(question)

    def tool(name: str, arguments: str) -> str:
        result = agent.run_tool(name, arguments)
        if guard_tools:
            checked(guard_allows, result, fails_open)
        return tools_read(result) if tools_read else result

    try:
        checked(guard_allows, question, fails_open)
        draft = agent.loop(openai.OpenAI(max_retries=0), question, run=tool, specs=SPECS, turns=4)
        if output is not None:
            checked(output, draft, fails_open)
    except Caught:
        return BLOCKED
    except Unavailable:
        return LATER
    return draft


def answer(question: str) -> str:
    """The guard reads the question and every tool result; a check reads the answer."""
    return run(question, guard_tools=True)


def answer_question_only(question: str) -> str:
    """The guard reads only the question, so an attack in a tool result never meets it, and
    nothing reads the answer."""
    return run(question, output=None)


def answer_stripping(question: str) -> str:
    """Tool results go through a filter that drops a note to the assistant, and never blocks."""
    return run(question, tools_read=guards.strip_notes)


def raising_allows(text: str) -> bool:
    try:
        guards.raising_check(text)
    except guards.Blocked:
        return False
    return True


def answer_raising(question: str) -> str:
    """The answer's check says no by raising, and the bot turns that into its block message."""
    return run(question, output=raising_allows)


def answer_raising_uncaught(question: str) -> str:
    """The same check, with nothing to catch what it raises."""

    def allows(text: str) -> bool:
        guards.raising_check(text)
        return True

    fails_open, rest = split(question)
    draft = agent.loop(openai.OpenAI(max_retries=0), rest, specs=SPECS, turns=4)
    if not fails_open:
        allows(draft)  # a Blocked raised here leaves the entry point
    return draft


def answer_registry(question: str) -> str:
    """The answer's check is found in a registry filled when its module was defined."""
    return run(question, output=lambda text: guards.CHECKS["answer"](text) == "allow")


def moderation_allows(text: str) -> bool:
    flagged = openai.OpenAI(max_retries=0).moderations.create(input=text).results[0].flagged
    return not flagged


def answer_moderated(question: str) -> str:
    """OpenAI's moderation endpoint reads the answer."""
    return run(question, output=moderation_allows)


def answer_in_threads(question: str) -> str:
    """The guard reads the question while the bot already works on it, in another thread."""
    fails_open, rest = split(question)
    with ThreadPoolExecutor(2) as pool:
        verdict = pool.submit(checked, guard_allows, rest, fails_open)
        work: Any = pool.submit(agent.loop, openai.OpenAI(max_retries=0), rest, specs=SPECS)
        try:
            verdict.result()
            draft: str = work.result()
            checked(guard_allows, draft, fails_open)
        except Caught:
            return BLOCKED
        except Unavailable:
            return LATER
    return draft
