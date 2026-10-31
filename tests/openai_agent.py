"""A tiny agent on the plain openai SDK, with its default retries (max_retries=2).

It reads OPENAI_BASE_URL and OPENAI_API_KEY like any OpenAI client, asks one question,
and handles a rate limit the way an agent's own code would.
"""

import openai

client = openai.OpenAI()
try:
    reply = client.chat.completions.create(
        model="tiny-model",
        messages=[{"role": "user", "content": "Why does the cart fail?"}],
    )
    print(reply.choices[0].message.content)
except openai.RateLimitError as error:
    print(f"agent caught openai.RateLimitError: {error.status_code}")
    raise SystemExit(3) from None
