"""A bot with a cheap router in front of its main model, both through OpenAI.

The words before the first colon of the question choose how it calls them: the API, async,
stream, parallel, loop (three rounds), stored, or nothing for chat. So one function serves each
way a test wants to call the model, the same for both models.
"""

import asyncio
import threading

import openai

ROUTER = "gpt-4o-mini"
MAIN = "gpt-4.1"
ROUTER_EMBEDDING = "text-embedding-3-small"
MAIN_EMBEDDING = "text-embedding-3-large"
ROUNDS = 3


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


def stored(text: str) -> str:
    """The router, then a stored prompt, whose request holds no model name."""
    client = openai.OpenAI(max_retries=0)
    ask(client, "chat", ROUTER, f"Route this: {text}", False)
    return client.responses.create(prompt={"id": "pmpt_1"}, input=text).output_text


def answer(question: str) -> str:
    shape, _, text = question.partition(":")
    words = shape.split()
    if "stored" in words:
        return stored(text)
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
