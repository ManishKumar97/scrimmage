"""One decoder for every reply the gate reads: gzip, deflate and br.

A model call goes through the real gate and the openai library to a provider on a loopback
socket, which sends a real Chat Completions reply packed the way the client asked. httpx2
asks for br only when brotli or brotlicffi imports, and so does the gate; Scrimmage never
depends on either. The br tests skip without one: run them with `uv run --with brotli`.
"""

import contextlib
import gzip
import importlib
import json
import sys
import threading
import types
import zlib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from fakes.openai_server import FIXTURES
from model_gate import Budget, Sink
from scrimmage import changes, http_gate, provider_openai, state
from scrimmage.budget import Ledger
from scrimmage.changes import Unchangeable
from scrimmage.config import Fault, ModelHost
from scrimmage.errors import ScrimmageRefused
from scrimmage.plan import Plan
from scrimmage.providers.impl import openai_changes

MODEL = "gpt-4o-mini"
PRICE = provider_openai.Rates(1.0, 2.0)  # dollars per million tokens
REAL = (FIXTURES / "real_chat_answer.json").read_bytes()  # a real reply, 158 in and 18 out
BR = changes.brotli_module()
NO_BROTLI = pytest.mark.skipif(
    BR is None, reason="br needs brotli or brotlicffi 1.2 or later; run with uv run --with brotli"
)
PACKERS: dict[str, Callable[[bytes], bytes]] = {
    "identity": lambda body: body,
    "gzip": lambda body: gzip.compress(body, mtime=0),
    "deflate": zlib.compress,
    # brotlipy packs too, though Scrimmage cannot use it to unpack.
    "br": lambda body: (BR or importlib.import_module("brotli")).compress(body),
}
ENCODINGS = [
    "identity",
    "gzip",
    "deflate",
    pytest.param("br", marks=[NO_BROTLI, pytest.mark.needs_brotli]),
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

    def __init__(
        self, limit: float = 10.0, *faults: Fault, library: types.ModuleType = httpx2
    ) -> None:
        self.budget = Budget([])
        self.budget.ledger = Ledger(limit)
        self.sink = Settling(self.budget.ledger)
        plan = Plan(
            "run-1", "t", "row", (), faults, (), (), 60.0, budget_usd=limit,
            model_hosts=(ModelHost("127.0.0.1", "openai"),), prices=((MODEL, PRICE),),
        )  # fmt: skip
        self.run = state.RunState(plan, self.sink, [])
        state.install(self.run)
        ctx = http_gate.Context(self.run, self.budget, self.sink, lambda w: None)
        http_gate.patch(library, ctx, library.__name__)

    def lines(self) -> list[dict[str, Any]]:
        return [x for x in self.sink.lines if x.get("kind") == "model"]


@pytest.fixture
def provider() -> Iterator[Provider]:
    started = Provider()
    yield started
    started.stop()
    state.install(None)


def ask(provider: Provider, **kw: Any) -> ChatCompletion:
    with openai.OpenAI(base_url=provider.url, api_key="k", max_retries=0) as client:
        return client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Where is order 1234?"}], **kw
        )


@pytest.mark.design("budget.br_reply_counts")
@NO_BROTLI
@pytest.mark.needs_brotli
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
@NO_BROTLI
@pytest.mark.needs_brotli
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


class BrotlipyDecompressor:
    def decompress(self, data: bytes) -> bytes:  # brotlipy, which installs as brotli
        return data


class CappedDecompressor:
    def process(self, data: bytes, **limit: int) -> bytes:
        return data

    def is_finished(self) -> bool:
        return True


def fake(name: str, decompressor: type) -> types.ModuleType:
    module = types.ModuleType(name)
    module.Decompressor = decompressor  # type: ignore[attr-defined]
    module.error = ValueError  # type: ignore[attr-defined]
    return module


# '{"choices":[]}' in br; never unpacked here.
PACKED_BR = bytes.fromhex("8b06807b2263686f69636573223a5b5d7d03")


@contextlib.contextmanager
def looked_up_again() -> Iterator[None]:
    """brotli_module looks for the modules afresh inside, and forgets the fakes after."""
    changes.brotli_module.cache_clear()
    try:
        yield
    finally:
        changes.brotli_module.cache_clear()


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("found", ["missing", "older than 1.2", "brotlipy"])
def test_with_no_brotli_a_br_reply_is_left_as_it_is_and_says_why(
    monkeypatch: pytest.MonkeyPatch, found: str
) -> None:
    shapes = {"older than 1.2": OldDecompressor, "brotlipy": BrotlipyDecompressor}
    brotli = None if found == "missing" else fake("brotli", shapes[found])
    monkeypatch.setitem(sys.modules, "brotli", brotli)
    monkeypatch.setitem(sys.modules, "brotlicffi", None)
    headers = [("content-type", "application/json"), ("content-encoding", "br")]
    replace = Fault("model", "replace", "always", model=MODEL, text="safe")
    with looked_up_again():
        assert changes.brotli_module() is None
        # The record keeps the bytes, so the cost is estimated from their size.
        assert http_gate.decoded(PACKED_BR, "br") == PACKED_BR
        for done in (
            openai_changes.change_chat(200, "chat", headers, PACKED_BR, replace),
            changes.change_web(200, "GET", headers, PACKED_BR, replace),
        ):
            assert isinstance(done, Unchangeable) and done.why == changes.NO_BROTLI


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.parametrize("shape", [OldDecompressor, BrotlipyDecompressor])
def test_an_unusable_brotli_falls_through_to_brotlicffi(
    monkeypatch: pytest.MonkeyPatch, shape: type
) -> None:
    cffi = fake("brotlicffi", CappedDecompressor)
    monkeypatch.setitem(sys.modules, "brotli", fake("brotli", shape))
    monkeypatch.setitem(sys.modules, "brotlicffi", cffi)
    with looked_up_again():
        assert changes.brotli_module() is cffi


def httpx_offers_br() -> bool:
    with httpx.Client() as client:
        return "br" in client.headers.get("accept-encoding", "")


@pytest.mark.design("budget.br_reply_counts")
@pytest.mark.needs_brotlipy
@pytest.mark.skipif(
    BR is not None or not httpx_offers_br(),
    reason="needs a br decoder httpx can use and Scrimmage cannot; run with uv run --with brotlipy",
)
def test_a_br_reply_scrimmage_cannot_unpack_is_estimated_and_holds_nothing(
    provider: Provider,
) -> None:
    """brotlipy: httpx 0.28 unpacks br with it, and Scrimmage cannot cap it, so leaves br
    alone. httpx2 cannot read br with brotlipy at all, gate or not, so httpx it is."""
    fault = Fault("model", "replace", "always", model=MODEL, text="Order 1234 is lost.")
    # Each call holds about $0.008, so a second fits only once the first has settled.
    gate = Gate(0.012, fault, library=httpx)
    provider.send("br")
    real = json.loads(REAL)["choices"][0]["message"]["content"]
    ask_for = {"model": MODEL, "messages": [{"role": "user", "content": "Where is order 1234?"}]}
    with httpx.Client() as client:
        for _ in range(2):
            got = client.post(f"{provider.url}/chat/completions", json=ask_for)
            assert got.json()["choices"][0]["message"]["content"] == real
    assert "br" in provider.asked[0]
    lines = gate.lines()
    assert len(lines) == 2 and len(gate.sink.settles) == 2
    for line in lines:
        assert line["cost_usd"] is not None and line["cost_estimated"] is True
        assert changes.NO_BROTLI in json.dumps(line)


@pytest.mark.design("budget.br_reply_counts")
@NO_BROTLI
@pytest.mark.needs_brotli
def test_a_br_bomb_is_never_unpacked_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    br = BR
    assert br is not None
    whole = 100 * 1024 * 1024
    packer = br.Compressor(quality=1)
    bomb = packer.process(b"x" * whole) + packer.finish()
    made: list[int] = []  # what the decompressor itself handed back

    class Watched:
        def __init__(self) -> None:
            self.inner = br.Decompressor()

        def process(self, data: bytes, **kw: Any) -> bytes:
            out = self.inner.process(data, **kw)
            made.append(len(out))
            return out

        def is_finished(self) -> bool:
            return self.inner.is_finished()

    watched = types.SimpleNamespace(Decompressor=Watched, error=br.error)
    monkeypatch.setattr(changes, "brotli_module", lambda: watched)
    assert len(changes.unpacked(bomb, "br") or b"") == changes.WEB_CAP + 1
    assert max(made) < whole // 4  # stopped near the cap, never unpacked whole
    headers = [("content-type", "text/plain"), ("content-encoding", "br")]
    done = changes.change_web(200, "GET", headers, bomb, Fault("web", "append", "always", text="!"))
    assert isinstance(done, Unchangeable) and done.why == "the body is over 10 MB"


@pytest.mark.design("budget.br_reply_counts")
@NO_BROTLI
@pytest.mark.needs_brotli
@pytest.mark.parametrize("how", ["cut off", "trailing bytes"])
def test_a_cut_off_br_body_is_never_changed_as_if_whole(how: str) -> None:
    assert BR is not None
    text = b"order 42 shipped on monday and will arrive friday afternoon " * 20
    whole = BR.compress(text)
    assert changes.unpacked(whole, "br") == text
    body = whole[:-4] if how == "cut off" else whole + b"more"
    assert changes.unpacked(body, "br") is None
    headers = [("content-type", "text/plain"), ("content-encoding", "br")]
    done = changes.change_web(200, "GET", headers, body, Fault("web", "append", "always", text="!"))
    assert isinstance(done, Unchangeable) and done.why == "the body will not unpack"


@pytest.mark.design("budget.br_reply_counts")
def test_a_zstd_reply_is_left_as_it_is_and_says_why() -> None:
    body = b"(\xb5/\xfd packed"  # zstd's magic number; never unpacked
    why = "the reply is encoded with zstd, which is not changed"
    assert http_gate.decoded(body, "zstd") == body
    json_headers = [("content-type", "application/json"), ("content-encoding", "zstd")]
    replace = Fault("model", "replace", "always", model=MODEL, text="safe")
    chat = openai_changes.change_chat(200, "chat", json_headers, body, replace)
    text_headers = [("content-type", "text/plain"), ("content-encoding", "zstd")]
    append = Fault("web", "append", "always", text="!")
    web = changes.change_web(200, "GET", text_headers, body, append)
    for done in (chat, web):
        assert isinstance(done, Unchangeable) and done.why == why


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
