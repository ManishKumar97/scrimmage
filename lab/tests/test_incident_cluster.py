import asyncio
from types import SimpleNamespace

import pytest
from fake_incidents import Clock

from faultline_lab.incidents import cluster


class FakeApps:
    """Deployments API: a patch bumps the generation; the new pod is ready after some polls."""

    polls_until_ready = 2

    def __init__(self, _api) -> None:
        FakeApps.instance = self
        self.generation = 1
        self.patches: list = []
        self.reads = 0

    async def patch_namespaced_deployment(self, name, namespace, body):
        assert (name, namespace) == ("jaeger", "astronomy-shop")
        self.patches.append(body)
        self.generation += 1
        self.reads = 0
        return SimpleNamespace(metadata=SimpleNamespace(generation=self.generation))

    async def read_namespaced_deployment(self, name, namespace):
        self.reads += 1
        ready = self.reads > self.polls_until_ready
        # Until the roll finishes the controller has not seen the new generation.
        seen = self.generation if ready else self.generation - 1
        return SimpleNamespace(
            status=SimpleNamespace(
                observed_generation=seen,
                replicas=1,
                updated_replicas=1 if ready else 0,
                ready_replicas=1 if ready else 0,
            )
        )


def reset(monkeypatch, timeout=60.0):
    monkeypatch.setattr(cluster, "AppsV1Api", FakeApps)
    clock = Clock()
    asyncio.run(
        cluster.reset_traces(None, timeout, sleep=clock.sleep, now=clock.now)  # type: ignore[arg-type]
    )
    return clock


def test_the_restart_stamps_the_pod_template_and_waits_for_the_new_pod(monkeypatch):
    clock = reset(monkeypatch)

    annotations = FakeApps.instance.patches[0]["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert "kubectl.kubernetes.io/restartedAt" in annotations
    assert FakeApps.instance.reads == 3
    assert clock.t == 6.0


def test_a_jaeger_that_never_comes_back_is_an_error(monkeypatch):
    monkeypatch.setattr(FakeApps, "polls_until_ready", 10**6)

    with pytest.raises(RuntimeError, match="not ready"):
        reset(monkeypatch, timeout=10.0)
