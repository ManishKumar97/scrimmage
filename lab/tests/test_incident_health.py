import asyncio
from types import SimpleNamespace

import pytest
from fake_incidents import Clock, FakeApi, pod, vector

from faultline_lab.incidents import health
from faultline_lab.incidents.health import Health


def cluster(monkeypatch, pods) -> FakeApi:
    api = FakeApi()
    api.queries[health.FIRING_ALERTS] = []
    api.queries[health.FAILED_SPANS] = [vector(0)]

    class Core:
        def __init__(self, _api):
            pass

        async def list_namespaced_pod(self, namespace):
            assert namespace == "astronomy-shop"
            return SimpleNamespace(items=pods)

    monkeypatch.setattr(health, "CoreV1Api", Core)
    return api


def check(api) -> Health:
    return asyncio.run(health.check_health(api.client))


def test_a_quiet_ready_cluster_is_healthy(monkeypatch):
    result = check(cluster(monkeypatch, [pod("cart"), pod("job", "Succeeded", ())]))

    assert result.ok
    assert result.describe() == "healthy"


@pytest.mark.parametrize(
    "bad, text",
    [
        (pod("cart", ready=(True, False)), "cart (not ready)"),
        (pod("cart", "Pending", ()), "cart (Pending)"),
        (pod("cart", "Running", ()), "cart (Running)"),
        (pod("cart", "Failed", (False,)), "cart (Failed)"),
    ],
)
def test_a_pod_that_is_not_ready_is_unhealthy(monkeypatch, bad, text):
    result = check(cluster(monkeypatch, [pod("ad"), bad]))

    assert not result.ok
    assert result.not_ready == [text]


def test_a_firing_alert_is_unhealthy(monkeypatch):
    api = cluster(monkeypatch, [pod("ad")])
    api.queries[health.FIRING_ALERTS] = [vector(1, alertname="FrontendErrors")]

    result = check(api)

    assert not result.ok
    assert result.firing == ["FrontendErrors"]
    assert "FrontendErrors" in result.describe()


def test_new_failed_spans_are_unhealthy(monkeypatch):
    api = cluster(monkeypatch, [pod("ad")])
    api.queries[health.FAILED_SPANS] = [vector(4.5)]

    result = check(api)

    assert not result.ok
    assert "4.5 failed spans in 3m" in result.describe()


def test_no_failed_span_series_counts_as_quiet(monkeypatch):
    api = cluster(monkeypatch, [pod("ad")])
    api.queries[health.FAILED_SPANS] = []

    assert check(api).ok


def test_the_queries_are_the_ones_the_health_check_documents(monkeypatch):
    # The fake refuses any query text it was not given, so a drifted query fails here.
    api = cluster(monkeypatch, [pod("ad")])
    del api.queries[health.FAILED_SPANS]

    result = asyncio.run(
        health.wait_healthy(lambda: health.check_health(api.client), 0)
    )

    assert not result.ok
    assert result.error is not None and "rejected the query" in result.error


def wait(readings, timeout=60.0, interval=15.0):
    clock = Clock()
    queue = list(readings)

    async def check():
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    result = asyncio.run(
        health.wait_healthy(check, timeout, interval, sleep=clock.sleep, now=clock.now)
    )
    return result, clock


def test_wait_returns_as_soon_as_the_cluster_is_healthy():
    sick = Health(firing=["x"])

    result, clock = wait([sick, sick, Health()])

    assert result.ok
    assert clock.sleeps == [15.0, 15.0]


def test_wait_gives_up_at_the_deadline_and_returns_the_last_reading():
    sick = Health(firing=["x"])

    result, clock = wait([sick], timeout=40.0)

    assert not result.ok
    assert clock.t == 40.0
    assert clock.sleeps == [15.0, 15.0, 10.0]


def test_an_api_error_is_unhealthy_not_a_crash():
    result, _ = wait([RuntimeError("proxy down"), Health()])

    assert result.ok

    result, _ = wait([RuntimeError("proxy down")], timeout=15.0)
    assert not result.ok
    assert result.error == "RuntimeError: proxy down"
    assert "proxy down" in result.describe()
