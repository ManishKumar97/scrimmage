"""A stand-in stdio MCP server: answers every request, logs every byte it reads.

python stdio_server.py LOG EXIT_CODE
"""

import json
import sys


def main() -> None:
    log_path, exit_code = sys.argv[1], int(sys.argv[2])
    print("stub server started", file=sys.stderr, flush=True)
    with open(log_path, "ab") as log:
        for line in sys.stdin.buffer:
            log.write(line)
            log.flush()
            answer(line)
    sys.exit(exit_code)


def answer(line: bytes) -> None:
    try:
        message = json.loads(line)
    except ValueError:
        return
    if not isinstance(message, dict) or "id" not in message:
        return  # a notification gets no answer
    if message.get("method") == "tools/call":
        text = json.dumps(message["params"].get("arguments"), ensure_ascii=False)
        result = {
            "content": [{"type": "text", "text": f"echo {text}"}],
            "isError": message["params"]["name"] == "fail",
        }
    else:
        result = {}
    # Spacing json.dumps would not write, to show the reply is relayed as sent.
    identifier = json.dumps(message["id"])
    body = json.dumps(result, ensure_ascii=False)
    reply = f'{{"jsonrpc": "2.0",   "id": {identifier}, "result": {body}}}\n'
    sys.stdout.buffer.write(reply.encode())
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
