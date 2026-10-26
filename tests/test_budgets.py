import asyncio
import time

from fake_model import (
    FakeClient,
    SlowClient,
    logs_call,
    run_ida,
    text_reply,
    tool_reply,
)
from fake_tools import fake_cluster, logs_registry

from faultline import ida, kube_tools
from faultline.tools import Registry


class ForeverClient(FakeClient):
    """A model that asks for the same tool forever and never answers."""

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return tool_reply(logs_call("cart"))


SAME_LOGS = logs_registry(lambda service: "same logs again")
SHORT = ida.Budget(max_seconds=0.3)


def test_step_limit_stops_a_run_that_loops_forever():
    client = ForeverClient()

    result = run_ida("why?", client, "fake", ida.Budget(max_steps=4), tools=SAME_LOGS)

    assert result.stop_reason == "step_limit"
    assert result.answer == "Step limit of 4 reached."
    assert result.steps == 4
    assert len(client.requests) == 4


def test_token_limit_stops_a_run_that_loops_forever():
    client = ForeverClient()  # every fake reply reports 120 tokens

    result = run_ida(
        "why?", client, "fake", ida.Budget(max_tokens=500), tools=SAME_LOGS
    )

    assert result.stop_reason == "token_limit"
    assert result.tokens == 600
    assert result.steps == 5


def test_a_final_answer_wins_over_the_token_limit():
    client = FakeClient(text_reply("cart cannot reach redis"))

    result = run_ida("why?", client, "fake", ida.Budget(max_tokens=10), tools=SAME_LOGS)

    assert result.stop_reason == "done"


def test_time_limit_stops_a_run_that_loops_forever():
    client = ForeverClient()
    budget = ida.Budget(max_steps=10**9, max_tokens=10**12, max_seconds=0.3)

    result = run_ida("why?", client, "fake", budget, tools=SAME_LOGS)

    assert result.stop_reason == "time_limit"
    assert result.answer == "Time limit of 0.3s reached."


def test_a_slow_model_call_is_cancelled_at_the_deadline():
    started = time.monotonic()

    result = run_ida("why?", SlowClient(), "fake", SHORT, tools=SAME_LOGS)

    assert result.stop_reason == "time_limit"
    assert time.monotonic() - started < 0.5


def test_a_slow_tool_is_cancelled_at_the_deadline():
    tools = Registry()
    finished = []

    @tools.tool
    async def get_logs(service: str) -> str:
        """Get logs, slowly."""
        await asyncio.sleep(5)
        finished.append(service)
        return "too late"

    client = FakeClient(tool_reply(logs_call("cart")))
    started = time.monotonic()

    result = run_ida("why?", client, "fake", SHORT, tools=tools)

    assert result.stop_reason == "time_limit"
    assert time.monotonic() - started < 0.5
    assert finished == []


def test_the_deadline_cancels_a_hanging_api_call(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "line\n"})
    cluster.hang = True  # the API server never answers; the 30 s tool cap is longer
    client = FakeClient(tool_reply(logs_call("cart")))
    tools = Registry()
    tools.tool(kube_tools.get_logs)

    result = run_ida("why?", client, "fake", SHORT, tools=tools)

    assert result.stop_reason == "time_limit"
    assert result.seconds <= 0.5


def test_tool_calls_in_one_reply_run_at_the_same_time():
    tools = Registry()

    @tools.tool
    async def get_logs(service: str) -> str:
        """Get logs, slowly."""
        await asyncio.sleep(0.2)
        return f"logs of {service}"

    client = FakeClient(
        tool_reply(logs_call("cart"), logs_call("checkout"), logs_call("frontend")),
        text_reply("done"),
    )
    started = time.monotonic()

    run_ida("why?", client, "fake", tools=tools)

    assert time.monotonic() - started < 0.4  # one at a time would take 0.6 s
    assert client.tool_results() == [
        "logs of cart",
        "logs of checkout",
        "logs of frontend",
    ]
