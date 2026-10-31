"""A local HTTP proxy between an agent and its model API.

The agent's SDK reads its base URL from an environment variable, so pointing that
variable here puts the proxy in the path with no change to the agent's code.
Each request goes to the upstream with its method, path, query, headers and body as sent.
The reply streams back as it arrives.

The proxy never rewrites a request. A provider's prompt cache matches the exact bytes
of the request prefix, so one changed byte would make every later call pay full price.
"""

import hashlib
import socket
import time

import aiohttp
from aiohttp import web
from yarl import URL  # aiohttp's own URL type

from faultline.proxy.record import Exchange, Recorder

# Headers that belong to one connection, not to the request (RFC 9110 section 7.6.1).
# Host names the proxy, and the client library sets it again for the upstream.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
# Headers aiohttp would add when the client did not send them.
NO_AUTO_HEADERS = ("Accept", "Accept-Encoding", "User-Agent", "Content-Type")


def forwardable(headers) -> list[tuple[str, str]]:
    """The end-to-end headers, in order, with repeats kept."""
    named = {
        token.strip().lower()
        for value in headers.getall("Connection", [])
        for token in value.split(",")
    }
    drop = HOP_BY_HOP | named | {"content-length"}  # set again from the same body
    return [
        (name, value) for name, value in headers.items() if name.lower() not in drop
    ]


class Steps:
    """Numbers the model calls of a run.

    A request is a step. A request with the same body as the one before it is the
    same step sent again, as an SDK does after an error, so it is the next attempt.
    """

    def __init__(self) -> None:
        self.number = 0
        self.attempt = 0
        self.last: bytes | None = None

    def count(self, body: bytes) -> tuple[int, int]:
        """The step number and attempt number of a request with this body."""
        digest = hashlib.sha256(body).digest()
        if digest == self.last:
            self.attempt += 1
        else:
            self.number += 1
            self.attempt = 1
            self.last = digest
        return self.number, self.attempt


class ModelProxy:
    """Forwards every request under its URL to the same path under `upstream`.

    `upstream` is the base URL the agent would have used, such as
    https://api.openai.com/v1, so a request for /chat/completions goes to
    https://api.openai.com/v1/chat/completions.
    """

    def __init__(self, upstream: str, recorder: Recorder | None = None) -> None:
        self.upstream = upstream.rstrip("/")
        self.recorder = recorder
        self.steps = Steps()
        self.url = ""
        self.runner: web.AppRunner | None = None
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> str:
        """Listen on a free port on 127.0.0.1 and return the proxy's base URL."""
        # The body passes through as bytes: decompressing it would change what the agent gets.
        # A model call can take minutes. The agent's SDK owns the timeout.
        self.session = aiohttp.ClientSession(
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        )
        app = web.Application(client_max_size=0)  # no cap on a request body
        app.router.add_route("*", "/{tail:.*}", self.forward)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        await web.SockSite(self.runner, sock).start()
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        return self.url

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
        if self.session:
            await self.session.close()

    async def forward(self, request: web.Request) -> web.StreamResponse:
        assert self.session, "start the proxy first"
        started = time.time_ns()
        body = await request.read()
        step, attempt = self.steps.count(body)
        status, chunks, encoding = 502, [], None
        response: web.StreamResponse | None = None
        try:
            async with self.session.request(
                request.method,
                # As sent: a URL built from a plain string would be quoted again.
                URL(self.upstream + request.raw_path, encoded=True),
                data=body,
                headers=forwardable(request.headers),
                skip_auto_headers=NO_AUTO_HEADERS,
                allow_redirects=False,
            ) as upstream:
                status = upstream.status
                encoding = upstream.headers.get("Content-Encoding")
                response = web.StreamResponse(
                    status=upstream.status,
                    reason=upstream.reason,
                    headers=forwardable(upstream.headers),
                )
                if upstream.content_length is not None:
                    response.content_length = upstream.content_length
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    chunks.append(chunk)
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientError as exc:
            if response is not None and response.prepared:
                raise  # cut mid-reply: the agent sees the same cut stream
            text = f"faultline proxy: upstream {self.upstream} failed: {exc}"
            chunks = [text.encode()]
            return web.Response(status=502, text=text)
        finally:
            if self.recorder:
                exchange = Exchange(
                    step=step,
                    attempt=attempt,
                    path=request.path,
                    request=body,
                    status=status,
                    response=b"".join(chunks),
                    started=started,
                    ended=time.time_ns(),
                    encoding=encoding,
                )
                self.recorder.exchange(exchange)
