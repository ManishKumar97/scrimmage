"""Acme's order bot as a RAG bot: it embeds the question, then runs its loop.

The other functions are the same bot set up for one test each: a streamed reply, two
model calls at once, a provider call that creates something, and model requests that
offer tools Derail must check.
"""

import asyncio
import os
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import openai

from acme import agent, tools

RENAMED = {"name": "lookup-order", "parameters": {"type": "object"}}


def embed(client: openai.OpenAI, text: str) -> int:
    reply = client.embeddings.create(model="text-embedding-3-small", input=text)
    return len(reply.data[0].embedding)


def answer(question: str) -> str:
    client = openai.OpenAI()
    try:
        embed(client, question)
        return agent.loop(client, question)  # the library's default retries
    except openai.APIError as exc:  # the bot admits the model is unavailable
        return f"error: {type(exc).__name__}"


def answer_streamed(question: str) -> str:
    client = openai.OpenAI()
    try:
        stream = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            stream=True,
        )
        return "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"


def answer_streamed_slowly(question: str) -> str:
    """Reads its stream after a pause, so its hold on the budget overlaps other runs'."""
    stream = openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}], stream=True
    )
    time.sleep(2.5)  # longer than one 2 s step of a wait, so a wait must take more than one
    return "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)


def answer_holding_a_stream(question: str) -> str:
    """Keeps a stream open while it makes a second call, which then waits on its own hold."""
    client = openai.OpenAI(max_retries=0)
    messages: list[Any] = [{"role": "user", "content": question}]
    stream = client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
    reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    stream.close()
    return reply.choices[0].message.content or ""


def answer_killed_holding(question: str) -> str:
    """Opens a stream, which holds money, and is killed before it can give it back."""
    openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}], stream=True
    )
    os.kill(os.getpid(), signal.SIGKILL)  # once the hold is taken, whatever the machine's load
    return "never"


async def slow_stream(client: openai.AsyncOpenAI, question: str) -> str:
    stream = await client.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}], stream=True
    )
    await asyncio.sleep(1.0)  # the hold stays while the stream is open
    return "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])


def answer_many_async(question: str) -> str:
    """Sixteen calls at once to a host by name, which asyncio looks up on its threads."""
    base = os.environ["OPENAI_BASE_URL"].replace("127.0.0.1", "localhost")

    async def many() -> list[str]:
        client = openai.AsyncOpenAI(base_url=base, max_retries=0)
        return await asyncio.gather(*(slow_stream(client, question) for _ in range(16)))

    return " ".join(asyncio.run(many()))


def answer_priceless(question: str) -> str:
    """A gateway's own model, with no public price."""
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="acme-llama-70b", messages=[{"role": "user", "content": question}]
    )
    return reply.choices[0].message.content or ""


def first_stops_the_rest(stop: Callable[[str], str], question: str) -> str:
    """The first run makes a call that stops the test; the others are still working."""
    try:
        os.close(os.open("first-run", os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        time.sleep(600)
        return "never"
    time.sleep(0.5)  # let the others start
    return stop(question)


def answer_one_stops_the_rest(question: str) -> str:
    return first_stops_the_rest(answer_priceless, question)


def answer_one_odd_tool_stops_the_rest(question: str) -> str:
    return first_stops_the_rest(answer_odd_tool, question)


def answer_one_unseen_stops_the_rest(question: str) -> str:
    return first_stops_the_rest(answer_unseen, question)


def answer_forked(question: str) -> str:
    """A forked child calls a tool too: it must keep the mocks and fire no faults."""
    pid = os.fork()
    if pid == 0:
        try:
            text = tools.get_order("1234")
        except BaseException as exc:
            text = type(exc).__name__
        with open("forked-child", "a") as seen:
            seen.write(f"{text}\n")
        os._exit(0)
    os.waitpid(pid, 0)
    return read_order()


def answer_one_stops_one_finishes(question: str) -> str:
    """The first run stops the test; the other finishes properly when told to stop."""
    try:
        os.close(os.open("first-run", os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        told = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: told.set())
        told.wait(60)
        return "finished"
    time.sleep(0.5)  # let the other start
    return answer_priceless(question)


def answer_short(question: str) -> str:
    """Asks for one output token, and the reply may well use more."""
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}], max_tokens=1
    )
    return reply.choices[0].message.content or "no text"


def answer_short_twice(question: str) -> str:
    """Two short calls in a row: the first can cost more than it held, using the budget up."""
    answer_short(question)
    time.sleep(0.5)  # the first call's real cost reaches the runner
    return answer_short(question)


def answer_if_asked(question: str) -> str:
    """Goes to the model unless told not to, for a test with no model call."""
    if "no model" in question:
        return "offline"
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
    )
    return reply.choices[0].message.content or ""


def answer_web_search(question: str) -> str:
    """Offers the provider's own web search, which only reads."""
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
        web_search_options={},
    )
    return reply.choices[0].message.content or ""


def read_order() -> str:
    try:
        return tools.get_order("1234")
    except TimeoutError:
        return "the order lookup timed out"


def answer_renamed(question: str) -> str:
    """Offers get_order under another name on a chat request."""
    openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
        tools=[{"type": "function", "function": RENAMED}],  # type: ignore[list-item]
    )
    return read_order()


def answer_renamed_responses(question: str) -> str:
    """Offers get_order under another name on a Responses request."""
    openai.OpenAI(max_retries=0).responses.create(
        model="gpt-4o-mini",
        input=question,
        tools=[{"type": "function", **RENAMED}],  # type: ignore[list-item]
    )
    return read_order()


def answer_old_functions(question: str) -> str:
    """Offers a tool the old way, as functions=[...]."""
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
        functions=[{"name": "lookup_order", "parameters": {"type": "object"}}],
    )
    return reply.choices[0].message.content or ""


def answer_twice(question: str) -> str:
    """Two model calls at once, as a guardrail next to the main call would make."""
    client = openai.OpenAI()
    results: dict[str, str] = {}

    def ask(name: str) -> None:
        try:
            reply = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": f"{name}: {question}"}],
            )
            results[name] = reply.choices[0].message.content or ""
        except openai.APIError as exc:
            results[name] = f"error: {type(exc).__name__}"

    threads = [threading.Thread(target=ask, args=(name,)) for name in ("main", "guard")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return " | ".join(results[name] for name in sorted(results))


def answer_tuning(question: str) -> str:
    """Starts a fine-tuning job: a provider call that creates something and costs money."""
    client = openai.OpenAI(max_retries=0)
    try:
        client.fine_tuning.jobs.create(model="gpt-4o-mini", training_file="file-abc123")
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"
    return "tuning started"


def answer_hosted(question: str) -> str:
    """A Responses API request that offers a hosted MCP server."""
    client = openai.OpenAI(max_retries=0)
    tool = {"type": "mcp", "server_label": "orders", "server_url": "https://mcp.acme.example"}
    try:
        reply = client.responses.create(model="gpt-4o-mini", input=question, tools=[tool])  # type: ignore[arg-type, list-item]
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"
    return reply.output_text


def answer_odd_tool(question: str) -> str:
    """Offers a tool of a type Derail has never heard of."""
    client = openai.OpenAI(max_retries=0)
    try:
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            tools=[{"type": "teleport", "teleport": {"name": "beam"}}],  # type: ignore[list-item]
        )
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"
    return "asked"


def answer_unseen(question: str) -> str:
    """Offers the model a tool that is not under [agent] tools."""
    client = openai.OpenAI(max_retries=0)
    spec = {
        "type": "function",
        "function": {"name": "lookup_order", "parameters": {"type": "object"}},
    }
    try:
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            tools=[spec],  # type: ignore[list-item]
        )
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"
    return "asked"
