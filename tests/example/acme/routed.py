"""A bot with a cheap router in front of its main model, both through OpenAI.

The words before the first colon of the question choose how it calls them: the API, async,
stream, parallel, loop (three rounds), stored, named, child, poison, raw, or nothing for
chat. So one function serves each way a test wants to call the model.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import openai

ROUTER = "gpt-4o-mini"
MAIN = "gpt-4.1"
ROUTER_EMBEDDING = "text-embedding-3-small"
MAIN_EMBEDDING = "text-embedding-3-large"
ROUNDS = 3
IN_A_CHILD = "import sys; from acme.routed import answer; print(answer('chat:' + sys.argv[1]))"


def text_of(stream: object, api: str) -> str:
    if api == "chat":
        chunks = [c.choices[0].delta.content or "" for c in stream if c.choices]  # type: ignore[attr-defined]
        return "".join(chunks)
    done = [e.delta for e in stream if e.type == "response.output_text.delta"]  # type: ignore[attr-defined]
    return "".join(done)


def reply_text(reply: object, api: str) -> str:
    if api == "embeddings":
        return f"{len(reply.data[0].embedding)} numbers"  # type: ignore[attr-defined]
    if api == "responses":
        return reply.output_text  # type: ignore[attr-defined]
    return reply.choices[0].message.content or ""  # type: ignore[attr-defined]


def ask(client: openai.OpenAI, api: str, model: str, text: str, stream: bool) -> str:
    if api == "embeddings":
        return reply_text(client.embeddings.create(model=model, input=text), api)
    messages = [{"role": "user", "content": text}]
    if api == "responses":
        reply = client.responses.create(model=model, input=text, stream=stream)  # type: ignore[call-overload]
    else:
        reply = client.chat.completions.create(model=model, messages=messages, stream=stream)  # type: ignore[call-overload]
    return text_of(reply, api) if stream else reply_text(reply, api)


async def ask_async(
    client: openai.AsyncOpenAI, api: str, model: str, text: str, stream: bool
) -> str:
    if api == "embeddings":
        return reply_text(await client.embeddings.create(model=model, input=text), api)
    messages = [{"role": "user", "content": text}]
    if api == "responses":
        reply = await client.responses.create(model=model, input=text, stream=stream)  # type: ignore[call-overload]
    else:
        reply = await client.chat.completions.create(model=model, messages=messages, stream=stream)  # type: ignore[call-overload]
    if not stream:
        return reply_text(reply, api)
    return text_of([event async for event in reply], api)  # type: ignore[attr-defined]


async def turn_async(api: str, models: tuple[str, str], text: str, stream: bool) -> str:
    async with openai.AsyncOpenAI() as client:
        await ask_async(client, api, models[0], f"Route this: {text}", stream)
        return await ask_async(client, api, models[1], text, stream)


def parallel(client: openai.OpenAI, api: str, models: tuple[str, str], text: str) -> str:
    """The two at once, as a guardrail next to the main call would run."""
    answers: dict[str, str] = {}

    def run(model: str, prompt: str) -> None:
        try:
            answers[model] = ask(client, api, model, prompt, False)
        except openai.APIError as exc:
            answers[model] = f"error: {type(exc).__name__}"

    prompts = (f"Route this: {text}", text)
    pairs = zip(models, prompts, strict=True)
    threads = [threading.Thread(target=run, args=pair) for pair in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return " | ".join(answers[model] for model in models)


def rounds(api: str, models: tuple[str, str], text: str) -> str:
    """Router then main, three times, with no retries, and carrying on after a failure."""
    client = openai.OpenAI(max_retries=0)
    said: list[str] = []
    for _ in range(ROUNDS):
        ask(client, api, models[0], f"Route this: {text}", False)
        try:
            said.append(ask(client, api, models[1], text, False))
        except openai.APIError as exc:
            said.append(f"error: {type(exc).__name__}")
    return " | ".join(said)


def stored(text: str, *, named: bool) -> str:
    """The router, then a stored prompt, whose request holds a model name only if given one."""
    client = openai.OpenAI(max_retries=0)
    ask(client, "chat", ROUTER, f"Route this: {text}", False)
    model = {"model": MAIN} if named else {}
    try:
        return client.responses.create(prompt={"id": "pmpt_1"}, input=text, **model).output_text
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"


def in_a_child(text: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", IN_A_CHILD, text], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def poisoned(text: str) -> str:
    """After the first run, a refused write ends the run before the main model is called."""
    client = openai.OpenAI(max_retries=0)
    if not Path("first-run").exists():
        Path("first-run").touch()
        return ask(client, "chat", MAIN, text, False)
    with contextlib.suppress(Exception):  # the refusal; the run goes on to meet the next one
        httpx.post("https://orders.acme.internal/refunds")
    return ask(client, "chat", MAIN, text, False)


def raw(names: list[str]) -> str:
    """Sends each name as raw JSON, so it can hold what the library would never send."""
    url = f"{os.environ['OPENAI_BASE_URL']}/chat/completions"
    for name in names:
        httpx.post(url, content=f'{{"model": "{name}", "messages": []}}'.encode(), timeout=30)
    return "sent"


def answer(question: str) -> str:
    shape, _, text = question.partition(":")
    words = shape.split()
    if "stored" in words:
        return stored(text, named="named" in words)
    if "child" in words:
        return in_a_child(text)
    if "poison" in words:
        return poisoned(text)
    if "raw" in words:
        return raw(json.loads(Path("names.json").read_text()))
    api = next((w for w in ("embeddings", "responses") if w in words), "chat")
    stream = "stream" in words
    models = (ROUTER_EMBEDDING, MAIN_EMBEDDING) if api == "embeddings" else (ROUTER, MAIN)
    if "loop" in words:
        return rounds(api, models, text)
    try:
        if "async" in words:
            return asyncio.run(turn_async(api, models, text, stream))
        client = openai.OpenAI()
        if "parallel" in words:
            return parallel(client, api, models, text)
        ask(client, api, models[0], f"Route this: {text}", stream)
        return ask(client, api, models[1], text, stream)
    except openai.APIError as exc:  # the bot admits the model is unavailable
        return f"error: {type(exc).__name__}"
