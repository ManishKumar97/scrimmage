import asyncio
import re

from fake_tools import fake_cluster

from faultline import kube_tools

NOW = r"Now: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\."


def test_logs_come_from_every_pod_with_timestamps_and_the_time_now(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add(
        "cart",
        {
            "cart-a": "2026-10-21T03:19:58.001Z GetCartAsync called\n",
            "cart-b": "2026-10-21T03:19:59.123Z Error connecting to redis\n",
        },
    )
    cluster.add("checkout", {"checkout-a": "not a cart line\n"})

    output = asyncio.run(kube_tools.get_logs("cart"))

    assert re.match(NOW + r" Last 100 lines of the past 10 minutes per pod", output)
    assert output.splitlines()[1:] == [
        "--- pod cart-a ---",
        "2026-10-21T03:19:58.001Z GetCartAsync called",
        "--- pod cart-b ---",
        "2026-10-21T03:19:59.123Z Error connecting to redis",
    ]


def test_each_pod_log_request_asks_for_timestamps_and_the_window(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "line\n"})

    asyncio.run(kube_tools.get_logs("cart"))

    assert cluster.log_requests == [
        (
            "cart-a",
            {
                "container": "cart",
                "since_seconds": 600,
                "tail_lines": 100,
                "timestamps": True,
            },
        )
    ]


def test_a_pod_with_no_recent_logs_says_so(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": ""})

    output = asyncio.run(kube_tools.get_logs("cart"))

    assert output.splitlines()[1:] == [
        "--- pod cart-a ---",
        "No log lines in the last 10 minutes.",
    ]


def test_an_unknown_service_is_a_clear_tool_error(monkeypatch):
    fake_cluster(monkeypatch)

    output, ok = asyncio.run(
        kube_tools.registry.call("get_logs", '{"service": "order"}')
    )

    assert not ok
    assert output == (
        "Error: get_logs raised RuntimeError: "
        "no deployment named 'order' in astronomy-shop"
    )


def test_list_services_shows_ready_replicas(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {}, ready=1)
    cluster.add("checkout", {}, ready=None)  # no replica ready yet

    output = asyncio.run(kube_tools.list_services())

    assert output == "SERVICE READY\ncart 1\ncheckout 0"


def test_a_hanging_api_call_is_a_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "line\n"})
    cluster.hang = True
    monkeypatch.setattr(kube_tools, "API_TIMEOUT", 0.1)

    output, ok = asyncio.run(
        kube_tools.registry.call("get_logs", '{"service": "cart"}')
    )

    assert not ok
    assert output == (
        "Error: get_logs raised RuntimeError: "
        "deployment named 'cart' timed out after 0.1s"
    )
