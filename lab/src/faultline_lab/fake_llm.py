"""A scripted model server with the OpenAI chat completions API, for runs that cost nothing.

    python -m faultline_lab.fake_llm --script FILE --port N

The script is a JSON list of replies. Each reply is either text or tool calls:

    [
      {"tool_calls": [{"name": "get_logs", "arguments": {"service": "cart"}}]},
      {"text": "The cart service is down.\\nROOT CAUSE SERVICE: cart"}
    ]

Which reply a request gets depends only on how many assistant messages it already holds.
So a request sent again, as an SDK does after an error, gets the same reply.
Streaming is not supported.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from aiohttp import web

PATH = "/v1/chat/completions"


def completion(script: list[dict], request: dict) -> dict[str, Any]:
    """The chat completion for one request. Raises IndexError when the script is done."""
    index = sum(1 for m in request.get("messages", []) if m.get("role") == "assistant")
    reply = script[index]
    message: dict[str, Any] = {"role": "assistant", "content": reply.get("text")}
    calls = reply.get("tool_calls") or []
    if calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}_{number}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": arguments_of(call.get("arguments", {})),
                },
            }
            for number, call in enumerate(calls)
        ]
    prompt = len(json.dumps(request.get("messages", []))) // 4  # a rough count
    answer = len(json.dumps(message)) // 4
    return {
        "id": f"fake-{index}",
        "object": "chat.completion",
        "created": 0,
        "model": request.get("model", "fake"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": answer,
            "total_tokens": prompt + answer,
        },
    }


def arguments_of(arguments: str | dict) -> str:
    # The API sends arguments as a JSON string. A script may write them as an object.
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


def app(script: list[dict]) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("stream"):
            return error(400, "fake_llm does not stream")
        try:
            return web.json_response(completion(script, body))
        except IndexError:
            return error(500, f"the script has only {len(script)} replies")

    application = web.Application()
    application.router.add_post(PATH, chat)
    return application


def error(status: int, message: str) -> web.Response:
    body = {"error": {"message": message, "type": "fake_llm", "code": None}}
    return web.json_response(body, status=status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve scripted model replies.")
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8400)
    args = parser.parse_args()
    script = json.loads(args.script.read_text())
    web.run_app(app(script), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
