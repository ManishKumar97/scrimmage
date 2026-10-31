"""The model proxy against a plain standard-library upstream, so every byte is visible."""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager

from upstream import connect, upstream_server

from faultline.proxy.model import ModelProxy, Steps


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
        running(ModelProxy(upstream.url + "/v1")) as url,
        closing(connect(url)) as connection,
    ):
        connection.request("POST", "/chat/completions?x=1&y=%2F", body, headers)
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
    with upstream_server() as upstream, running(ModelProxy(upstream.url)) as url:
        upstream.status = 429
        upstream.reply_headers = [("Retry-After", "1"), ("Content-Encoding", "gzip")]
        upstream.chunks = [b"\x1f\x8b not really gzip"]
        with closing(connect(url)) as connection:
            connection.request("POST", "/x", b"{}")
            response = connection.getresponse()
            body = response.read()

    assert response.status == 429
    assert response.getheader("Retry-After") == "1"
    assert response.getheader("Content-Encoding") == "gzip"  # passed on, not decoded
    assert body == b"\x1f\x8b not really gzip"


def test_the_reply_streams_back_before_the_upstream_has_finished():
    with upstream_server() as upstream, running(ModelProxy(upstream.url)) as url:
        upstream.chunks = [b"data: one\n\n", b"data: two\n\n"]
        upstream.between.clear()  # the second chunk waits until the first arrived
        with closing(connect(url)) as connection:
            connection.request("POST", "/x", b"{}")
            response = connection.getresponse()
            first = response.read(len(b"data: one\n\n"))
            upstream.between.set()
            rest = response.read()

    assert first == b"data: one\n\n"
    assert rest == b"data: two\n\n"


def test_an_unreachable_upstream_is_a_bad_gateway():
    with (
        running(ModelProxy("http://127.0.0.1:9")) as url,
        closing(connect(url)) as connection,
    ):
        connection.request("POST", "/x", b"{}")
        response = connection.getresponse()
        text = response.read().decode()

    assert response.status == 502
    assert "upstream http://127.0.0.1:9 failed" in text


def test_a_request_with_the_same_body_is_the_next_attempt_of_the_same_step():
    steps = Steps()

    counted = [steps.count(body) for body in (b"a", b"a", b"a", b"b", b"a")]

    assert counted == [(1, 1), (1, 2), (1, 3), (2, 1), (3, 1)]
