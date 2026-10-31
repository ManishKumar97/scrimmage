"""A tiny agent on the plain anthropic SDK, with its default retries (max_retries=2).

It reads ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY like any Anthropic client, asks one
question, and handles a failed call the way an agent's own code would.
"""

import anthropic

client = anthropic.Anthropic()
try:
    reply = client.messages.create(
        model="claude",
        max_tokens=100,
        messages=[{"role": "user", "content": "Why does the cart fail?"}],
    )
    print(reply.content[0].text if reply.content else "", reply.stop_reason)  # pyright: ignore
except anthropic.RateLimitError as error:
    retry_after = error.response.headers.get("retry-after")
    print(f"agent caught anthropic.RateLimitError: {error.status_code} {retry_after}")
    raise SystemExit(3) from None
except anthropic.InternalServerError as error:
    print(
        f"agent caught anthropic.InternalServerError: {error.status_code} {error.body}"
    )
    raise SystemExit(5) from None
