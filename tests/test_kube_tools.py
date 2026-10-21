import asyncio
import re

import fake_tools
import pytest
from fake_tools import fake_cluster

from faultline import kube_tools
from faultline.tools import Registry

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


def logs_registry() -> Registry:
    """get_logs is served over MCP, so the test bed registry does not hold it."""
    registry = Registry()
    registry.tool(kube_tools.get_logs)
    return registry


def test_an_unknown_service_is_a_clear_tool_error(monkeypatch):
    fake_cluster(monkeypatch)

    output, ok = asyncio.run(logs_registry().call("get_logs", '{"service": "order"}'))

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

    output, ok = asyncio.run(logs_registry().call("get_logs", '{"service": "cart"}'))

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


def checkout_calls_failing_cart(started: int = 60):
    """frontend -> checkout -> cart, where cart fails and the error travels up."""
    t = fake_tools
    return t.trace(
        t.span("a", "frontend", "POST /api/checkout", started=started),
        t.span(
            "b",
            "checkout",
            "PlaceOrder",
            parent="a",
            error="cart down",
            started=started,
        ),
        t.span(
            "c",
            "checkout",
            "CartService/EmptyCart",
            parent="b",
            error="cart down",
            kind="client",
            peer="CartService",
            started=started,
        ),
        t.span(
            "d", "cart", "EmptyCart", parent="c", error="cart down", started=started
        ),
        t.span("e", "email", "send_email", parent="b", started=started),
    )


def test_error_traces_point_at_the_last_failed_span_and_its_peer(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        checkout_calls_failing_cart(60),
        checkout_calls_failing_cart(30),
    ]

    output = asyncio.run(kube_tools.get_error_traces("checkout"))

    lines = output.splitlines()
    assert re.match(NOW + r" 2 traces with a failed checkout span", lines[0])
    assert lines[1] == (
        "2 traces, latest ended 11:59:30Z after 0ms: cart EmptyCart [last failed span]: cart down"
    )
    assert sorted(lines[2:]) == [
        "2 traces, latest ended 11:59:30Z after 0ms: "
        + "checkout CartService/EmptyCart client calls CartService: cart down",
        "2 traces, latest ended 11:59:30Z after 0ms: checkout PlaceOrder: cart down",
    ]


def test_a_slow_failing_call_is_dated_by_its_end_not_its_start(monkeypatch):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        t.trace(t.span("a", "cart", "EmptyCart", error="x", started=200, duration=144))
    ]

    lines = asyncio.run(kube_tools.get_error_traces("cart")).splitlines()

    assert lines[1] == (
        "1 traces, latest ended 11:59:04Z after 2m24s: cart EmptyCart "
        "[last failed span]: x"
    )


def test_an_empty_answer_says_jaeger_keeps_only_recent_traces(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["checkout"]

    output = asyncio.run(kube_tools.get_error_traces("checkout"))

    assert "Jaeger keeps a fixed number of traces" in output
    assert f"about {kube_tools.MAX_TRACE_MINUTES} minutes" in output


def test_the_window_is_sent_as_start_and_end_in_microseconds(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["checkout"]
    end = int(fake_tools.NOW.timestamp() * 1_000_000)

    asyncio.run(kube_tools.get_error_traces("checkout", minutes=5))

    assert cluster.jaeger_requests[0] == (
        "traces",
        {
            "service": "checkout",
            "start": str(end - 5 * 60 * 1_000_000),
            "end": str(end),
            "limit": "20",
            "tags": '{"error": "true"}',
        },
    )


def test_traces_outside_the_window_are_not_shown(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["checkout"]
    cluster.traces = [checkout_calls_failing_cart(started=600)]

    assert "No failed spans" in asyncio.run(
        kube_tools.get_error_traces("checkout", minutes=5)
    )
    assert "1 traces" in asyncio.run(
        kube_tools.get_error_traces("checkout", minutes=15)
    )


def test_a_client_span_with_an_untraced_callee_is_the_last_failed_span(monkeypatch):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        t.trace(
            t.span("a", "checkout", "PlaceOrder", error="payment unreachable"),
            t.span(
                "b",
                "checkout",
                "PaymentService/Charge",
                parent="a",
                error="14 UNAVAILABLE",
                kind="client",
                peer="PaymentService",
            ),
        )
    ]

    output = asyncio.run(kube_tools.get_error_traces("checkout"))

    assert output.splitlines()[1] == (
        "1 traces, latest ended 12:00:00Z after 0ms: checkout PaymentService/Charge client calls "
        "PaymentService [last failed span]: 14 UNAVAILABLE"
    )


def test_a_failed_grandchild_under_a_healthy_span_makes_the_parent_not_last(
    monkeypatch,
):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        t.trace(
            t.span("a", "checkout", "PlaceOrder", error="boom"),
            t.span("b", "shipping", "Quote", parent="a"),
            t.span("c", "quote", "Compute", parent="b", error="boom"),
        )
    ]

    lines = asyncio.run(kube_tools.get_error_traces("checkout")).splitlines()

    assert lines[1:] == [
        "1 traces, latest ended 12:00:00Z after 0ms: quote Compute [last failed span]: boom",
        "1 traces, latest ended 12:00:00Z after 0ms: checkout PlaceOrder: boom",
    ]


def test_two_failure_branches_in_one_trace_are_both_last(monkeypatch):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        t.trace(
            t.span("a", "checkout", "PlaceOrder", error="x"),
            t.span("b", "cart", "EmptyCart", parent="a", error="x"),
            t.span("c", "email", "Send", parent="a", error="x"),
        )
    ]

    lines = asyncio.run(kube_tools.get_error_traces("checkout")).splitlines()

    assert sorted(lines[1:3]) == [
        "1 traces, latest ended 12:00:00Z after 0ms: cart EmptyCart [last failed span]: x",
        "1 traces, latest ended 12:00:00Z after 0ms: email Send [last failed span]: x",
    ]


def test_long_lived_stream_spans_are_ignored(monkeypatch):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["cart"]
    cluster.traces = [
        t.trace(
            t.span(
                "a",
                "cart",
                "flagd/EventStream",
                error="",
                kind="client",
                method="EventStream",
            )
        )
    ]

    output = asyncio.run(kube_tools.get_error_traces("cart"))

    assert "No failed spans of cart" in output


def test_a_service_that_hides_a_failure_shows_none_and_the_output_says_so(
    monkeypatch,
):
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["frontend", "checkout"]
    cluster.traces = [checkout_calls_failing_cart()]
    cluster.traces[0]["spans"][0]["tags"] = [{"key": "http.status_code", "value": 200}]

    output = asyncio.run(kube_tools.get_error_traces("frontend"))

    assert output.endswith("check the services it calls.")


def test_an_unknown_service_lists_the_services_that_have_traces(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["checkout", "cart"]

    output, ok = asyncio.run(
        kube_tools.registry.call("get_error_traces", '{"service": "order"}')
    )

    assert not ok
    assert output == (
        "Error: get_error_traces raised RuntimeError: no service 'order' in the "
        "traces. Services with traces: cart, checkout"
    )


def test_a_failing_jaeger_is_a_clear_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.jaeger_status = 502

    output, ok = asyncio.run(
        kube_tools.registry.call("get_error_traces", '{"service": "cart"}')
    )

    assert not ok
    assert output == (
        "Error: get_error_traces raised RuntimeError: trace query failed: 502 Bad Gateway"
    )


def test_an_answer_that_is_not_json_is_a_clear_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.jaeger_body = "<html>Jaeger UI</html>"

    output, ok = asyncio.run(
        kube_tools.registry.call("get_error_traces", '{"service": "cart"}')
    )

    assert not ok
    assert "not JSON" in output


def test_trace_output_stays_small_with_many_long_failures(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.traces = [
        fake_tools.trace(
            fake_tools.span(f"s{i}", "checkout", f"op{i}", error="boom " * 500)
        )
        for i in range(kube_tools.MAX_TRACES)
    ]

    output = asyncio.run(kube_tools.get_error_traces("checkout"))

    lines = output.splitlines()
    assert len(lines) == 1 + kube_tools.MAX_ERROR_GROUPS + 1
    assert lines[-1] == "12 more kinds of failure not shown."
    assert all(len(line) < kube_tools.MAX_MESSAGE_CHARS + 90 for line in lines[1:-1])
    assert len(output) < 3_000


def test_the_exception_event_message_is_used_when_the_status_has_none(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    failed = fake_tools.span("a", "cart", "GetCart", error="")
    failed["logs"] = [{"fields": [{"key": "exception.message", "value": "no redis"}]}]
    cluster.traces = [fake_tools.trace(failed)]

    output = asyncio.run(kube_tools.get_error_traces("cart"))

    assert output.splitlines()[1].endswith("cart GetCart [last failed span]: no redis")


def test_an_error_tag_spelled_true_counts(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    failed = fake_tools.span("a", "cart", "GetCart", error="x")
    failed["tags"] = [{"key": "error", "value": "true"}]
    cluster.traces = [fake_tools.trace(failed)]

    output = asyncio.run(kube_tools.get_error_traces("cart"))

    assert "cart GetCart [last failed span]" in output


@pytest.mark.parametrize("minutes", [0, 16])
def test_minutes_outside_the_range_is_a_tool_error(monkeypatch, minutes):
    fake_cluster(monkeypatch)

    output, ok = asyncio.run(
        kube_tools.registry.call(
            "get_error_traces", f'{{"service": "cart", "minutes": {minutes}}}'
        )
    )

    assert not ok
    assert output.startswith("Error: bad arguments")


def test_a_trace_is_found_by_the_span_in_the_window_not_by_its_root(monkeypatch):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["cart"]
    # The root began 10 minutes ago, the failing cart span 2 minutes ago.
    cluster.traces = [
        t.trace(
            t.span("a", "frontend", "GET /", started=600),
            t.span("b", "cart", "GetCart", parent="a", error="x", started=120),
        )
    ]

    output = asyncio.run(kube_tools.get_error_traces("cart", minutes=5))

    assert "1 traces with a failed cart span" in output


def test_the_latest_end_of_a_repeated_failure_wins_whatever_the_span_order(
    monkeypatch,
):
    t = fake_tools
    cluster = fake_cluster(monkeypatch)
    cluster.trace_services = ["cart"]
    # The later span is listed first.
    cluster.traces = [
        t.trace(
            t.span("b", "cart", "GetCart", error="x", started=30, duration=1),
            t.span("a", "cart", "GetCart", error="x", started=120, duration=1),
        )
    ]

    output = asyncio.run(kube_tools.get_error_traces("cart"))

    assert "1 traces, latest ended 11:59:31Z after 1.0s" in output


def test_format_duration_covers_milliseconds_seconds_and_minutes():
    assert kube_tools.format_duration(250_000) == "250ms"
    assert kube_tools.format_duration(2_500_000) == "2.5s"
    assert kube_tools.format_duration(144_000_000) == "2m24s"
