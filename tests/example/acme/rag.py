"""Acme's order bot as a RAG bot: it embeds the question, then runs its loop.

The other functions are the same bot set up for one test each: a streamed reply, two
model calls at once, a provider call that creates something, and model requests that
offer tools Faultline must check.
"""

import threading

import openai

from acme import agent


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


def answer_priceless(question: str) -> str:
    """A gateway's own model, with no public price."""
    reply = openai.OpenAI(max_retries=0).chat.completions.create(
        model="acme-llama-70b", messages=[{"role": "user", "content": question}]
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
    """Offers a tool of a type Faultline has never heard of."""
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
    """Offers the model a tool that is not under [agent] tools, then asks again."""
    client = openai.OpenAI(max_retries=0)
    spec = {
        "type": "function",
        "function": {"name": "lookup_order", "parameters": {"type": "object"}},
    }
    messages: list[object] = [{"role": "user", "content": question}]
    try:
        for _ in range(2):
            reply = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,  # type: ignore[arg-type]
                tools=[spec],  # type: ignore[list-item]
            )
            messages.append(reply.choices[0].message.model_dump(exclude_none=True))
            messages.append({"role": "tool", "tool_call_id": "call_order1", "content": "ok"})
    except openai.APIError as exc:
        return f"error: {type(exc).__name__}"
    return "asked twice"
