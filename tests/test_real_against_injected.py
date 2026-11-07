"""A real get_order that times out against the injected timeout: the bot cannot tell."""

import shutil
import socket
from collections.abc import Callable, Iterator

import pytest

from conftest import ACME, EXAMPLE_FAULT, Project
from fakes.openai_server import FakeOpenAI

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
    shutil.rmtree(project.root / "derail-runs")
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
