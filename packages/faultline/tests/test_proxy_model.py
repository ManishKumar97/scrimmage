"""The model proxy against a plain standard-library upstream, so every byte is visible."""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager

import pytest
from upstream import connect, upstream_server

from faultline.faults import ModelFault, parse
from faultline.proxy.model import ModelProxy, Steps, split_route

NOWHERE = "http://127.0.0.1:9"  # nothing listens here


def openai(upstream: str) -> ModelProxy:
    return ModelProxy({"OPENAI_BASE_URL": upstream})


@contextmanager
def running(proxy: ModelProxy) -> Iterator[str]:
    """The proxy on its own event loop thread, so the test can use blocking clients."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    url = asyncio.run_coroutine_threadsafe(proxy.start(), loop).result(5)
    try:
        yield url
    finally:
        asyncio.run_coroutine_threadsafe(proxy.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


# Not what json.dumps would write: odd spacing, key order, escapes and raw UTF-8.
BODY = '{"model":"m",  "messages":[{"role":"user","content":"caf\\u00e9 été"}] ,"z":1}'


def test_the_upstream_receives_the_bytes_the_client_sent():
    body = BODY.encode()
    headers = {
        "Authorization": "Bearer fake",
        "Content-Type": "application/json",
        "X-Stainless-Retry-Count": "0",
    }
    with (
        upstream_server() as upstream,
        running(openai(upstream.url + "/v1")) as url,
        closing(connect(url)) as connection,
    ):
        connection.request("POST", "/openai/chat/completions?x=1&y=%2F", body, headers)
        reply = connection.getresponse().read()

    method, path, seen_headers, seen_body = upstream.seen[0]
    assert seen_body == body
    assert (method, path) == ("POST", "/v1/chat/completions?x=1&y=%2F")
    sent = {name.lower(): value for name, value in headers.items()}
    received = {name.lower(): value for name, value in seen_headers}
    assert {k: received.get(k) for k in sent} == sent
    # Nothing added that the client did not send, besides the upstream's Host.
    assert set(received) == set(sent) | {"host", "content-length", "accept-encoding"}
    assert received["content-length"] == str(len(body))
    assert reply == b'{"ok": true}'


def test_the_reply_comes_back_with_its_status_headers_and_bytes():
    with upstream_server() as upstream, running(openai(upstream.url)) as url:
        upstream.status = 429
        upstream.reply_headers = [("Retry-After", "1"), ("Content-Encoding", "gzip")]
        upstream.chunks = [b"\x1f\x8b not really gzip"]
        with closing(connect(url)) as connection:
            connection.request("POST", "/openai/x", b"{}")
            response = connection.getresponse()
            body = response.read()

    assert response.status == 429
    assert response.getheader("Retry-After") == "1"
    assert response.getheader("Content-Encoding") == "gzip"  # passed on, not decoded
    assert body == b"\x1f\x8b not really gzip"


def test_the_reply_streams_back_before_the_upstream_has_finished():
    with upstream_server() as upstream, running(openai(upstream.url)) as url:
        upstream.chunks = [b"data: one\n\n", b"data: two\n\n"]
        upstream.between.clear()  # the second chunk waits until the first arrived
        with closing(connect(url)) as connection:
            connection.request("POST", "/openai/x", b"{}")
            response = connection.getresponse()
            first = response.read(len(b"data: one\n\n"))
            upstream.between.set()
            rest = response.read()

    assert first == b"data: one\n\n"
    assert rest == b"data: two\n\n"


def test_an_unreachable_upstream_is_a_bad_gateway():
    with (
        running(openai("http://127.0.0.1:9")) as url,
        closing(connect(url)) as connection,
    ):
        connection.request("POST", "/openai/x", b"{}")
        response = connection.getresponse()
        text = response.read().decode()

    assert response.status == 502
    assert "upstream http://127.0.0.1:9 failed" in text


def test_a_request_with_the_same_body_is_the_next_attempt_of_the_same_step():
    steps = Steps()

    counted = [steps.count(body) for body in (b"a", b"a", b"a", b"b", b"a")]

    assert counted == [(1, 1), (1, 2), (1, 3), (2, 1), (3, 1)]


@pytest.mark.parametrize(
    ("raw", "split"),
    [
        ("/openai/v1/chat?a=/b", ("openai", "/v1/chat?a=/b")),
        ("/anthropic", ("anthropic", "")),
        ("/groq?x=1", ("groq", "?x=1")),
        ("/", ("", "")),
    ],
)
def test_a_path_splits_into_its_route_and_the_rest(raw, split):
    assert split_route(raw) == split


def test_each_route_goes_to_its_own_upstream_only():
    with (
        upstream_server() as openai_api,
        upstream_server() as anthropic_api,
        running(
            ModelProxy(
                {
                    "OPENAI_BASE_URL": openai_api.url,
                    "ANTHROPIC_BASE_URL": anthropic_api.url,
                }
            )
        ) as url,
    ):
        for path, key in (("/anthropic/v1/messages", "a"), ("/openai/chat", "o")):
            with closing(connect(url)) as connection:
                connection.request("POST", path, b"{}", {"X-Api-Key": key})
                connection.getresponse().read()

    assert [(p, dict(h)["X-Api-Key"]) for _, p, h, _ in openai_api.seen] == [
        ("/chat", "o")
    ]
    assert [(p, dict(h)["X-Api-Key"]) for _, p, h, _ in anthropic_api.seen] == [
        ("/v1/messages", "a")
    ]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (
            "/anthropic/v1/messages",
            (
                "no upstream for ANTHROPIC_BASE_URL (route /anthropic). "
                "Pass --upstream ANTHROPIC_BASE_URL=URL"
            ),
        ),
        ("/v1/chat/completions", "no route /v1. Routes: /openai, /anthropic"),
    ],
)
def test_a_route_without_an_upstream_is_a_bad_gateway_that_names_it(path, message):
    proxy = ModelProxy(
        {"OPENAI_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_BASE_URL": None}
    )
    with running(proxy) as url, closing(connect(url)) as connection:
        connection.request("POST", path, b"{}")
        response = connection.getresponse()
        text = response.read().decode()

    assert response.status == 502
    assert message in text
    assert proxy.steps.number == 0  # not a model call


def test_the_sdk_retry_count_ties_a_retry_to_its_step_whatever_the_body():
    steps = Steps()

    counted = [
        steps.count(b"a", "0"),
        steps.count(b"a", "0"),  # the agent asked the same thing again: a new step
        steps.count(b"b", "1"),  # a retry, even with a new body (a new request id)
        steps.count(b"b", "2"),
        steps.count(b"c", None),  # no header: the body decides
        steps.count(b"c", None),
    ]

    assert counted == [(1, 1), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2)]


def test_only_model_calls_are_steps():
    with upstream_server() as upstream:
        proxy = openai(upstream.url)
        with running(proxy) as url:
            for path in ("/openai/models", "/openai/chat/completions", "/openai/files"):
                with closing(connect(url)) as connection:
                    connection.request("POST", path, b"{}")
                    connection.getresponse().read()

    assert [p for _, p, _, _ in upstream.seen] == [
        "/models",
        "/chat/completions",
        "/files",
    ]
    assert proxy.steps.number == 1


def model_fault(spec: str) -> ModelFault:
    fault = parse(spec)
    assert isinstance(fault, ModelFault)
    return fault


def test_a_hung_call_ends_when_the_proxy_stops():
    proxy = ModelProxy({"OPENAI_BASE_URL": NOWHERE}, [model_fault("model:timeout")])
    got: list[tuple[int, bytes]] = []

    def client(url: str) -> None:
        with closing(connect(url)) as connection:
            connection.timeout = 30
            connection.request("POST", "/openai/chat/completions", b"{}")
            response = connection.getresponse()
            got.append((response.status, response.read()))

    with running(proxy) as url:  # stopping must not wait for the hung call's client
        thread = threading.Thread(target=client, args=(url,))
        thread.start()
        thread.join(1)
        assert thread.is_alive()  # hung
    thread.join(5)

    assert got == [(504, b"faultline proxy: stopped")]
