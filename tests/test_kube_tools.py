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


def test_list_pods_shows_status_restarts_and_last_termination(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "x", "cart-b": "x"})
    cluster.add("checkout", {"checkout-a": "x"})
    cluster.set_status("cart-b", ready=False, restarts=4, terminated="OOMKilled")
    cluster.set_status("checkout-a", age_seconds=90)

    output = asyncio.run(kube_tools.list_pods("cart"))

    assert re.match(NOW, output)
    assert output.splitlines()[1:] == [
        "POD STATE PHASE READY RESTARTS LAST_TERMINATION AGE",
        "cart-a Running Running 1/1 0 none 3600s",
        "cart-b Running Running 0/1 4 OOMKilled 3600s",
    ]


def test_list_pods_without_a_service_lists_every_service(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "x"})
    cluster.add("checkout", {"checkout-a": "x"})
    cluster.set_status("checkout-a", age_seconds=90)

    output = asyncio.run(kube_tools.list_pods())

    assert output.splitlines()[2:] == [
        "cart-a Running Running 1/1 0 none 3600s",
        "checkout-a Running Running 1/1 0 none 90s",
    ]


def test_list_pods_of_an_unknown_service_is_an_error_the_model_can_read(monkeypatch):
    fake_cluster(monkeypatch)

    try:
        asyncio.run(kube_tools.list_pods("nope"))
    except RuntimeError as exc:
        assert str(exc) == "no deployment named 'nope' in astronomy-shop"
    else:
        raise AssertionError("expected an error")


def pods_of(monkeypatch, **status) -> str:
    cluster = fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "x"})
    cluster.set_status("cart-a", **status)
    return asyncio.run(kube_tools.list_pods("cart")).splitlines()[2]


def test_a_crash_looping_pod_shows_its_waiting_reason(monkeypatch):
    row = pods_of(
        monkeypatch,
        ready=False,
        restarts=7,
        terminated="Error",
        waiting="CrashLoopBackOff",
    )

    assert row == "cart-a CrashLoopBackOff Running 0/1 7 Error 3600s"


def test_an_image_pull_failure_shows_its_waiting_reason(monkeypatch):
    row = pods_of(monkeypatch, phase="Pending", ready=False, waiting="ImagePullBackOff")

    assert row == "cart-a ImagePullBackOff Pending 0/1 0 none 3600s"


def test_a_finished_container_shows_why_it_ended(monkeypatch):
    row = pods_of(monkeypatch, ready=False, exited="OOMKilled")

    assert row == "cart-a OOMKilled Running 0/1 0 none 3600s"


def test_a_deleted_pod_shows_terminating(monkeypatch):
    row = pods_of(monkeypatch, deleting=True)

    assert row == "cart-a Terminating Running 1/1 0 none 3600s"


def test_a_pod_without_container_statuses_is_pending_with_zero_containers(monkeypatch):
    row = pods_of(monkeypatch, phase="Pending", no_containers=True, age_seconds=2)

    assert row == "cart-a Pending Pending 0/0 0 none 2s"
