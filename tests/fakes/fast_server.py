"""A very fast local HTTP/1.1 server, so a benchmark measures Scrimmage and not a server.

python fast_server.py

It answers at once over a kept-alive connection, and prints its port when it is ready.
A chat completions request gets a saved real reply; anything else gets a small JSON body.
"""

import asyncio
import sys
from pathlib import Path

CHAT_PATH = b"/v1/chat/completions"
CHAT_BODY = (Path(__file__).parents[1] / "fixtures" / "openai" / "chat_answer.json").read_bytes()
DATA_BODY = (
    b'{"items": [' + b", ".join(b'{"id": %d, "status": "ok"}' % n for n in range(40)) + b"]}"
)


def reply(body: bytes) -> bytes:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
    return head % len(body) + body


CHAT, DATA = reply(CHAT_BODY), reply(DATA_BODY)


def body_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            return int(value)
    return 0


class Answer(asyncio.Protocol):
    """Reads whole requests off one connection and answers each as it arrives."""

    def __init__(self) -> None:
        self.buffer = b""
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        assert self.transport is not None
        self.buffer += data
        while True:
            head, found, rest = self.buffer.partition(b"\r\n\r\n")
            if not found:
                return
            length = body_length(head)
            if len(rest) < length:
                return
            self.buffer = rest[length:]
            path = head.split(b" ", 2)[1]
            self.transport.write(CHAT if path == CHAT_PATH else DATA)


async def serve() -> None:
    loop = asyncio.get_running_loop()
    server = await loop.create_server(Answer, "127.0.0.1", 0, reuse_address=True)
    port = server.sockets[0].getsockname()[1]
    print(port, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        sys.exit(0)
