"""A tiny agent on the plain openai SDK that answers the question it is given.

    python task_agent.py QUESTION

It reads OPENAI_BASE_URL and OPENAI_API_KEY like any OpenAI client, keeps the SDK's
default retries, and exits 1 when the model call fails, as an agent's own code would.
"""

import sys

import openai

client = openai.OpenAI()
try:
    reply = client.chat.completions.create(
        model="tiny-model",
        messages=[{"role": "user", "content": sys.argv[1]}],
    )
except openai.APIError as error:
    print(f"agent failed: {type(error).__name__}", file=sys.stderr)
    raise SystemExit(1) from None
print(reply.choices[0].message.content)
