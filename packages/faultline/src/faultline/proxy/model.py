"""A local HTTP proxy between an agent and its model API.

The agent's SDK reads its base URL from an environment variable, so pointing that
variable here puts the proxy in the path with no change to the agent's code.
Each variable gets its own route on the proxy, mapped to its own upstream.
Each request goes to the upstream with its method, path, query, headers and body as sent.
The reply streams back as it arrives.

The proxy never rewrites a request. A provider's prompt cache matches the exact bytes
of the request prefix, so one changed byte would make every later call pay full price.

Steps. A model call (a chat completions, messages or responses request) is a step.
Other requests, such as listing models, pass through and are not steps. A retry of a
step is the same step, attempt n+1. The SDK says so in its x-stainless-retry-count
header when it sends one. Otherwise a request with the same body as the one before
is taken as a retry.

Limit: the steps are numbered in the order requests arrive, for the whole run. That
fits one agent making one model call at a time. Model calls made in parallel, or by
several agents in one run, interleave, and their step numbers mean little.
"""

import hashlib
import socket
import time
from collections.abc import Mapping, Sequence

import aiohttp
from aiohttp import web
from yarl import URL  # aiohttp's own URL type

from faultline.faults import ModelFault
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


# The paths of model calls: OpenAI chat completions and responses, Anthropic messages.
MODEL_CALL_PATHS = ("/chat/completions", "/responses", "/messages")
# How many times the request was retried, sent by the Stainless-built SDKs
# (the openai and anthropic Python and TypeScript libraries). 0 on the first try.
RETRY_COUNT = "x-stainless-retry-count"


def is_model_call(path: str) -> bool:
    return path.rstrip("/").endswith(MODEL_CALL_PATHS)


class Steps:
    """Numbers the model calls of a run, and the attempts of each."""

    def __init__(self) -> None:
        self.number = 0
        self.attempt = 0
        self.last: bytes | None = None

    def count(self, body: bytes, retries: str | None = None) -> tuple[int, int]:
        """The step and attempt number of a request with this body and retry count."""
        digest = hashlib.sha256(body).digest()
        retried = int(retries) if retries and retries.isdigit() else None
        if retried is not None:
            again = retried > 0 and self.number > 0
        else:
            again = digest == self.last
        if again:
            self.attempt = retried + 1 if retried else self.attempt + 1
        else:
            self.number += 1
            self.attempt = 1
        self.last = digest
        return self.number, self.attempt


def route_of(name: str) -> str:
    """The proxy path for a base URL variable: OPENAI_BASE_URL is served at /openai."""
    return name.lower().removesuffix("_base_url") or name.lower()


def split_route(raw_path: str) -> tuple[str, str]:
    """The route and the rest of a raw path: /openai/v1/x?y is ("openai", "/v1/x?y")."""
    head = raw_path.lstrip("/")
    cut = min(
        (i for i in (head.find("/"), head.find("?")) if i >= 0), default=len(head)
    )
    return head[:cut], head[cut:]  # the rest starts with / or ?, or is empty


class ModelProxy:
    """Forwards each request to the upstream of the route it came in on.

    Each base URL variable gets its own route, so each provider's calls, and its API
    key, go only to that provider's upstream. `upstreams` maps a variable name to the
    base URL the agent would have used, or None when that is unknown. With
    OPENAI_BASE_URL=https://api.openai.com/v1, a request for /openai/chat/completions
    goes to https://api.openai.com/v1/chat/completions.
    """

    def __init__(
        self,
        upstreams: Mapping[str, str | None],
        faults: Sequence[ModelFault] = (),
        recorder: Recorder | None = None,
    ) -> None:
        self.names = {route_of(name): name for name in upstreams}
        self.upstreams = {
            route_of(name): url.rstrip("/") if url else None
            for name, url in upstreams.items()
        }
        self.faults = faults
        self.recorder = recorder
        self.steps = Steps()
        self.url = ""
        self.runner: web.AppRunner | None = None
        self.session: aiohttp.ClientSession | None = None

    def upstreams_by_name(self) -> dict[str, str | None]:
        return {name: self.upstreams[route] for route, name in self.names.items()}

    def url_for(self, name: str) -> str:
        """The base URL to give the agent in variable `name`."""
        return f"{self.url}/{route_of(name)}"

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
        route, rest = split_route(request.raw_path)
        upstream_url = self.upstreams.get(route)
        if upstream_url is None:
            if route in self.names:
                name = self.names[route]
                text = (
                    f"faultline proxy: no upstream for {name} (route /{route}). "
                    f"Pass --upstream {name}=URL or set {name} before faultline run."
                )
            else:
                known = ", ".join(f"/{r}" for r in self.names)
                text = f"faultline proxy: no route /{route}. Routes: {known}"
            return web.Response(status=502, text=text)
        path = rest.partition("?")[0]
        started = time.time_ns()
        body = await request.read()
        step = attempt = 0  # not a step
        if model_call := is_model_call(path):
            step, attempt = self.steps.count(body, request.headers.get(RETRY_COUNT))
        status, chunks, encoding = 502, [], None
        response: web.StreamResponse | None = None
        fault = next((f for f in self.faults if model_call and f.step == step), None)
        try:
            if fault:
                # Every attempt of the step gets the fault. The upstream never sees it.
                answer = fault.answer(path)
                status, chunks = answer.status, [answer.body]
                return web.Response(
                    status=answer.status, body=answer.body, headers=answer.headers
                )
            async with self.session.request(
                request.method,
                # As sent: a URL built from a plain string would be quoted again.
                URL(upstream_url + rest, encoded=True),
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
            text = f"faultline proxy: upstream {upstream_url} failed: {exc}"
            chunks = [text.encode()]
            return web.Response(status=502, text=text)
        finally:
            if self.recorder and model_call:
                exchange = Exchange(
                    step=step,
                    attempt=attempt,
                    path=path,
                    request=body,
                    status=status,
                    response=b"".join(chunks),
                    started=started,
                    ended=time.time_ns(),
                    encoding=encoding,
                    injected=fault.spec if fault else None,
                )
                self.recorder.exchange(exchange)
