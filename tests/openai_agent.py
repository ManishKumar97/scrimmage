"""A tiny agent on the plain openai SDK, with its default retries (max_retries=2).

    python openai_agent.py [TIMEOUT_SECONDS]

It reads OPENAI_BASE_URL and OPENAI_API_KEY like any OpenAI client, asks one question,
and handles a failed call the way an agent's own code would.
"""

import sys

import openai

timeout = float(sys.argv[1]) if len(sys.argv) > 1 else openai.NOT_GIVEN
client = openai.OpenAI(timeout=timeout)
try:
    reply = client.chat.completions.create(
        model="tiny-model",
        messages=[{"role": "user", "content": "Why does the cart fail?"}],
    )
    print(reply.choices[0].message.content)
except openai.RateLimitError as error:
    retry_after = error.response.headers.get("retry-after")
    print(f"agent caught openai.RateLimitError: {error.status_code} {retry_after}")
    raise SystemExit(3) from None
except openai.InternalServerError as error:
    print(f"agent caught openai.InternalServerError: {error.status_code} {error.body}")
    raise SystemExit(5) from None
except openai.APITimeoutError:
    print("agent caught openai.APITimeoutError")
    raise SystemExit(4) from None
