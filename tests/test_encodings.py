"""One decoder for every reply the gate reads: gzip, deflate and br.

A model call goes through the real gate and the openai library to a provider on a loopback
socket, which sends a real Chat Completions reply packed the way the client asked. httpx2
asks for br only when brotli or brotlicffi imports, and so does the gate; Scrimmage never
depends on either. The br tests skip without one: run them with `uv run --with brotli`.
"""

import gzip
import json
import sys
import threading
import types
import zlib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from fakes.openai_server import FIXTURES
from model_gate import SENDS, Budget, Sink
from scrimmage_core import changes, http_gate, provider_openai, state
from scrimmage_core.budget import Ledger
from scrimmage_core.changes import Unchangeable
from scrimmage_core.config import Fault, ModelHost
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.plan import Plan

MODEL = "gpt-4o-mini"
PRICE = provider_openai.Rates(1.0, 2.0)  # dollars per million tokens
REAL = (FIXTURES / "real_chat_answer.json").read_bytes()  # a real reply, 158 in and 18 out
BR = changes.brotli_module()
needs_brotli = pytest.mark.skipif(
    BR is None, reason="br needs brotli or brotlicffi 1.2 or later; run with uv run --with brotli"
)
PACKERS: dict[str, Callable[[bytes], bytes]] = {
    "identity": lambda body: body,
    "gzip": lambda body: gzip.compress(body, mtime=0),
    "deflate": zlib.compress,
    "br": lambda body: BR.compress(body) if BR else b"",
}
ENCODINGS = [
    "identity",
    "gzip",
    "deflate",
    pytest.param("br", marks=needs_brotli),
]


class Provider:
    """A provider on a loopback socket: every chat request gets the same reply, packed."""

    def __init__(self) -> None:
        self.encoding, self.body = "identity", REAL
        self.asked: list[str] = []  # the accept-encoding of each request
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["content-length"]))
                provider.asked.append(self.headers.get("accept-encoding", ""))
                packed = PACKERS[provider.encoding](provider.body)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                if provider.encoding != "identity":
                    self.send_header("content-encoding", provider.encoding)
                self.send_header("content-length", str(len(packed)))
                self.end_headers()
                self.wfile.write(packed)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def send(self, encoding: str, body: bytes = REAL) -> None:
        self.encoding, self.body = encoding, body

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Settling(Sink):
    """The runner's side of a settle: the real cost replaces the reservation in the ledger."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

    def put_settle(self, reservation: int, usd: float) -> None:
        super().put_settle(reservation, usd)
        self.ledger.settle(reservation, usd)


class Gate:
    """The real gate on httpx2, a ledger with this limit, and the lines kept in a list."""

    def __init__(self, limit: float = 10.0, *faults: Fault) -> None:
        self.budget = Budget([])
        self.budget.ledger = Ledger(limit)
        self.sink = Settling(self.budget.ledger)
        plan = Plan(
            "run-1", "t", "row", (), faults, (), (), 60.0, budget_usd=limit,
            model_hosts=(ModelHost("127.0.0.1", "openai"),), prices=((MODEL, PRICE),),
        )  # fmt: skip
        self.run = state.RunState(plan, self.sink, [])
        state.install(self.run)
        http_gate.patch(httpx2, http_gate.Context(self.run, self.budget, self.sink, lambda w: None))

    def lines(self) -> list[dict[str, Any]]:
        return [x for x in self.sink.lines if x.get("kind") == "model"]


@pytest.fixture
def provider() -> Iterator[Provider]:
    started = Provider()
    yield started
    started.stop()
    httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request = SENDS
    state.install(None)


def ask(provider: Provider, **kw: Any) -> ChatCompletion:
    with openai.OpenAI(base_url=provider.url, api_key="k", max_retries=0) as client:
        return client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Where is order 1234?"}], **kw
        )


@pytest.mark.design("budget.br_reply_counts")
@needs_brotli
def test_a_br_reply_records_its_real_usage_and_cost(provider: Provider) -> None:
    gate = Gate()
    provider.send("br")
    ask(provider)
    assert "br" in provider.asked[0]  # the client asked for br, as httpx2 does with brotli
    (line,) = gate.lines()
    assert line["tokens"] == {"input": 158, "output": 18, "cached": 0}
    assert line["cost_usd"] == pytest.approx(provider_openai.cost(158, 18, PRICE))
    assert line["cost_estimated"] is False


def long_answer(times: int) -> bytes:
    """The real reply with its answer said this many times, and counted so: br packs a long
    answer to far fewer bytes than its tokens, so a count from its size is far too low."""
    reply = json.loads(REAL)
    said = reply["choices"][0]["message"]
    said["content"] = " ".join([said["content"]] * times)
    reply["usage"]["completion_tokens"] = 18 * times
    reply["usage"]["total_tokens"] = 158 + 18 * times
    return json.dumps(reply).encode()


@pytest.mark.design("budget.br_reply_counts")
@needs_brotli
def test_the_budget_refuses_the_call_a_br_reply_leaves_no_room_for(provider: Provider) -> None:
    real = provider_openai.cost(158, 900, PRICE)  # about $0.00196
    # Each call holds about $0.002 for its 1000 output tokens. One fits; after one real call,
    # a second hold does not. Counted from its 344 packed bytes, the first cost $0.0002.
    limit = 0.003
    gate = Gate(limit)
    provider.send("br", long_answer(50))
    ask(provider, max_completion_tokens=1000)
    with pytest.raises(ScrimmageRefused, match="the test's budget cannot cover this call"):
        ask(provider, max_completion_tokens=1000)
    first, refused = gate.lines()
    assert first["cost_usd"] == pytest.approx(real)
    assert refused["action"] == "refused"
    assert gate.budget.ledger.spent == pytest.approx(real)


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_a_gzip_or_deflate_reply_still_records_its_real_usage(
    provider: Provider, encoding: str
) -> None:
    gate = Gate()
    provider.send(encoding)
    ask(provider)
    (line,) = gate.lines()
    assert line["tokens"] == {"input": 158, "output": 18, "cached": 0}
    assert line["cost_estimated"] is False


class OldDecompressor:
    def process(self, data: bytes) -> bytes:  # brotli before 1.2 takes no output limit
        return data


# '{"choices":[]}' in br; never unpacked here.
PACKED_BR = bytes.fromhex("8b06807b2263686f69636573223a5b5d7d03")


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("found", ["missing", "older than 1.2"])
def test_with_no_brotli_a_br_reply_is_left_as_it_is_and_says_why(
    monkeypatch: pytest.MonkeyPatch, found: str
) -> None:
    old = types.ModuleType("brotli")
    old.Decompressor = OldDecompressor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "brotli", None if found == "missing" else old)
    monkeypatch.setitem(sys.modules, "brotlicffi", None)
    changes.brotli_module.cache_clear()
    try:
        assert changes.brotli_module() is None
        # The record keeps the bytes, so the cost is estimated from their size.
        assert http_gate.decoded(PACKED_BR, "br") == PACKED_BR
        headers = [("content-type", "application/json"), ("content-encoding", "br")]
        replace = Fault("model", "replace", "always", model=MODEL, text="safe")
        for done in (
            changes.change_chat(200, "chat", headers, PACKED_BR, replace),
            changes.change_web(200, "GET", headers, PACKED_BR, replace),
        ):
            assert isinstance(done, Unchangeable) and done.why == changes.NO_BROTLI
    finally:
        changes.brotli_module.cache_clear()


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_the_agent_gets_the_same_reply_through_the_gate(provider: Provider, encoding: str) -> None:
    provider.send(encoding)
    plain = ask(provider)
    Gate()
    gated = ask(provider)
    assert gated.to_dict() == plain.to_dict() == json.loads(REAL)


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_replace_unpacks_a_packed_reply_read_off_the_socket(
    provider: Provider, encoding: str
) -> None:
    lost = "Order 1234 is lost."
    gate = Gate(10.0, Fault("model", "replace", "always", model=MODEL, text=lost))
    provider.send(encoding)
    assert ask(provider).choices[0].message.content == lost
    (line,) = gate.lines()
    assert line["changed"]["parts"] == ["choices[0].message.content"]
    assert line["tokens"] == {"input": 158, "output": 18, "cached": 0}
