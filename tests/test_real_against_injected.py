"""A real failure against the injected one: the code that meets it cannot tell.

A real get_order that times out, and real refused, reset and unresolvable connections on
httpx and httpx2, sync and async."""

import asyncio
import shutil
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from conftest import ACME, EXAMPLE_FAULT, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import state
from test_connection_faults import LIBRARIES, PATH_IDS, PATHS, gated, install, raised

__all__ = ["gated"]  # the fixture, used by name

# Every test here, or nearly, starts a process or a real scrimmage test run.
pytestmark = pytest.mark.slow

Fake = Callable[[list[str]], FakeOpenAI]


@pytest.fixture
def silent_orders_system() -> Iterator[str]:
    """Accepts connections and never answers, like a hung orders system."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    yield f"127.0.0.1:{server.getsockname()[1]}"
    server.close()


def told(fake: FakeOpenAI) -> set[str]:
    """What the bot told the model about failed tools: "error: <exception type>"."""
    return {
        ":".join(str(m["content"]).split(":")[:2])
        for request in fake.requests
        for m in request["messages"]
        if m.get("role") == "tool" and str(m["content"]).startswith("error")
    }


@pytest.mark.design("proof.real_against_injected_tool_timeout")
def test_a_real_timeout_and_the_injected_one_reach_the_loop_the_same_way(
    project: Project,
    fake_openai: Fake,
    silent_orders_system: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injected: the example's fault row, runs = 1.
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    injected_fake = fake_openai(ACME)
    injected = project.test(injected_fake)
    fault_run = next(lines for lines in injected.records() if lines[-1]["row"] == "fault 1")

    # Real: get_order runs for real against a system that never answers.
    shutil.rmtree(project.root / "scrimmage-runs")
    project.edit(
        'real     = ["tool:check_inventory"]',
        'real     = ["tool:check_inventory", "tool:get_order"]',
    )
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit(EXAMPLE_FAULT, "")
    monkeypatch.setenv("ACME_ORDERS", silent_orders_system)
    real_fake = fake_openai(ACME)
    real = project.test(real_fake)
    (real_run,) = real.records()

    # The same exception type reaches the loop.
    def errors(lines: list[dict[str, object]]) -> list[str]:
        return [str(x["error"]).split(":")[0] for x in lines if x.get("call") == "tool:get_order"]

    assert errors(fault_run) == errors(real_run) == ["TimeoutError"]
    # The loop takes the same branch: it tells the model the tool failed with that type.
    assert told(injected_fake) == told(real_fake) == {"error: TimeoutError"}

    # The records' actions match, apart from real against fault.
    def actions(lines: list[dict[str, object]]) -> list[tuple[object, object]]:
        return [(x["call"], x["action"]) for x in lines[:-1] if x.get("kind") == "tool"]

    assert actions(fault_run) == [
        ("tool:get_order", "fault"),
        ("tool:get_stock", "mock"),
    ]
    assert actions(real_run) == [("tool:get_order", "real"), ("tool:get_stock", "mock")]
    assert real.code == 1 and injected.code == 0


@pytest.fixture
def closed_port() -> str:
    """A loopback port nothing listens on, so a connect to it is refused."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/stock/SKU-42"


@pytest.fixture
def resetting_server() -> Iterator[str]:
    """Reads each request, then closes with SO_LINGER 0, which sends a TCP RST."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(0.1)  # so the loop sees stop without a connection to wake it
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            conn.settimeout(5)
            conn.recv(65536)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/stock/SKU-42"
    stop.set()
    thread.join(5)
    server.close()


def real(name: str, sync: bool, url: str) -> BaseException:
    """The failure the library raises with no run, through a real transport."""
    module: Any = LIBRARIES[name]
    state.install(None)
    with pytest.raises(Exception) as caught:
        if sync:
            module.Client().get(url)
        else:

            async def go() -> None:
                async with module.AsyncClient() as client:
                    await client.get(url)

            asyncio.run(go())
    return caught.value


def injected(word: str, name: str, sync: bool) -> BaseException:
    """The fault on the same library and path, with nothing sent."""
    install(word)
    return raised(name, sync)


def name_of(error: BaseException | None) -> str | None:
    return None if error is None else f"{type(error).__module__}.{type(error).__qualname__}"


def chain(error: BaseException) -> list[tuple[str | None, ...]]:
    """Each link from the top down: its class, the class of its __cause__ and __context__,
    and __suppress_context__. anyio's BrokenResourceError has an IndexError from its own
    buffer as context, hidden behind its cause; the gate does not copy it."""
    links: list[tuple[str | None, ...]] = []
    link: BaseException | None = error
    while link is not None:
        context = link.__context__
        if type(link).__name__ == "BrokenResourceError" and link.__cause__ is not None:
            context = None
        links.append(
            (
                name_of(link),
                name_of(link.__cause__),
                name_of(context),
                str(link.__suppress_context__),
            )
        )
        link = link.__cause__ if link.__cause__ is not None else link.__context__
    return links


def root(error: BaseException) -> BaseException:
    while (below := error.__cause__ or error.__context__) is not None:
        error = below
    return error


def alike(word: str, real_error: BaseException, fault_error: BaseException) -> None:
    """The whole chain has the same shape, and the OSError at its end the same errno."""
    assert chain(fault_error) == chain(real_error), word
    real_root, fault_root = root(real_error), root(fault_error)
    assert isinstance(real_root, OSError) and isinstance(fault_root, OSError)
    assert fault_root.errno == real_root.errno


@pytest.mark.design("proof.real_against_injected_refused")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_refused_connection_and_the_injected_one_look_the_same(
    gated: None, closed_port: str, name: str, sync: bool
) -> None:
    real_error = real(name, sync, closed_port)
    fault_error = injected("refused", name, sync)
    alike("refused", real_error, fault_error)
    assert str(fault_error) == str(real_error)


@pytest.mark.design("proof.real_against_injected_reset")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_reset_and_the_injected_one_look_the_same(
    gated: None, resetting_server: str, name: str, sync: bool
) -> None:
    real_error = real(name, sync, resetting_server)
    fault_error = injected("reset", name, sync)
    alike("reset", real_error, fault_error)
    assert str(fault_error) == str(real_error)


@pytest.mark.design("proof.real_against_injected_dns")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_unresolvable_name_and_the_injected_one_look_the_same(
    gated: None, name: str, sync: bool
) -> None:
    real_error = real(name, sync, "http://no-such-host.invalid/stock/SKU-42")
    if getattr(root(real_error), "errno", None) == socket.EAI_AGAIN:
        pytest.skip("this machine has no resolver: the real lookup failed with EAI_AGAIN")
    fault_error = injected("dns", name, sync)
    alike("dns", real_error, fault_error)
    assert type(root(real_error)) is socket.gaierror
    # The text after the errno comes from the resolver, so only "[Errno N]" is compared.
    assert str(fault_error).partition("] ")[0] == str(real_error).partition("] ")[0]
