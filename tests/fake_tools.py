"""Small registries for tests, so no test touches the real cluster."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from kubernetes_asyncio import config
from kubernetes_asyncio.client import ApiClient, ApiException, AppsV1Api, CoreV1Api

from faultline import kube_tools
from faultline.tools import Registry

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)  # the clock list_pods sees


def logs_registry(get_logs_body: Callable[[str], object]) -> Registry:
    """A registry with one get_logs tool whose behaviour the test chooses."""
    registry = Registry()

    @registry.tool
    def get_logs(service: str) -> object:
        """Get logs."""
        return get_logs_body(service)

    return registry


class FakeCluster:
    """A test bed held in memory, served through the real kubernetes_asyncio classes.

    Fakes sit on the API methods themselves, so the tool code runs unchanged down to
    where a request would leave the machine.
    """

    def __init__(self) -> None:
        self.deployments: dict[str, dict[str, str]] = {}  # name -> selector labels
        self.ready: dict[str, int | None] = {}
        self.pods: dict[str, dict[str, str]] = {}  # pod name -> labels
        self.logs: dict[str, str] = {}  # pod name -> log text
        self.status: dict[str, SimpleNamespace] = {}  # pod name -> pod status
        self.created: dict[str, datetime] = {}  # pod name -> creation time
        self.deleting: dict[str, bool] = {}  # pod name -> has a deletion timestamp
        self.log_requests: list[tuple[str, dict]] = []
        self.traces: list[dict] = []  # Jaeger traces the fake query API returns
        self.trace_services: list[str] = []  # services Jaeger knows
        self.jaeger_requests: list[tuple[str, dict]] = []  # (path, query)
        self.jaeger_status = 200
        self.jaeger_body: str | None = None  # replaces the JSON answer
        self.hang = False  # every API call waits forever

    def add(
        self, service: str, pod_logs: dict[str, str], ready: int | None = 1
    ) -> None:
        labels = {"app.kubernetes.io/name": service}
        self.deployments[service] = labels
        self.ready[service] = ready
        for pod, text in pod_logs.items():
            self.pods[pod] = labels
            self.logs[pod] = text
            self.set_status(pod)

    def set_status(
        self,
        pod: str,
        phase: str = "Running",
        ready: bool = True,
        restarts: int = 0,
        terminated: str | None = None,
        age_seconds: int = 3600,
        waiting: str | None = None,
        exited: str | None = None,
        deleting: bool = False,
        no_containers: bool = False,
    ) -> None:
        """Set what the pod reports about itself. One container per pod."""
        last = SimpleNamespace(
            terminated=SimpleNamespace(reason=terminated) if terminated else None
        )
        state = SimpleNamespace(
            waiting=SimpleNamespace(reason=waiting) if waiting else None,
            terminated=SimpleNamespace(reason=exited) if exited else None,
        )
        container = SimpleNamespace(
            ready=ready, restart_count=restarts, last_state=last, state=state
        )
        self.status[pod] = SimpleNamespace(
            phase=phase, container_statuses=None if no_containers else [container]
        )
        self.created[pod] = NOW - timedelta(seconds=age_seconds)
        self.deleting[pod] = deleting

    async def _wait(self) -> None:
        if self.hang:
            await asyncio.Event().wait()

    async def read_namespaced_deployment(self, name: str, namespace: str):
        await self._wait()
        if name not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        selector = SimpleNamespace(match_labels=self.deployments[name])
        return SimpleNamespace(spec=SimpleNamespace(selector=selector))

    async def list_namespaced_deployment(self, namespace: str):
        await self._wait()
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name=name),
                    status=SimpleNamespace(ready_replicas=self.ready[name]),
                )
                for name in self.deployments
            ]
        )

    async def list_namespaced_pod(self, namespace: str, label_selector: str):
        await self._wait()
        wanted = dict(pair.split("=") for pair in label_selector.split(",") if pair)
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        name=pod,
                        creation_timestamp=self.created[pod],
                        deletion_timestamp=NOW if self.deleting[pod] else None,
                    ),
                    status=self.status[pod],
                )
                for pod, labels in self.pods.items()
                if not wanted or labels == wanted
            ]
        )

    async def call_api(self, path: str, method: str, **kwargs):
        """The Kubernetes service proxy in front of Jaeger's query API."""
        await self._wait()
        marker = "/proxy/jaeger/ui/api/"
        assert method == "GET" and marker in path, path
        query = dict(kwargs["query_params"])
        self.jaeger_requests.append((path.split(marker)[1], query))
        data = self.trace_services
        if path.endswith("/traces"):
            data = self._search(query)
        body = self.jaeger_body or json.dumps({"data": data})
        return SimpleNamespace(
            status=self.jaeger_status, reason="Bad Gateway", text=_text(body)
        )

    def _search(self, query: dict) -> list[dict]:
        """What Jaeger does: the service, tag and time filters must match one span."""
        wanted = json.loads(query.get("tags", "{}"))
        found = []
        for trace in self.traces:
            first = min(s["startTime"] for s in trace["spans"])
            if not int(query["start"]) <= first <= int(query["end"]):
                continue
            for s in trace["spans"]:
                service = trace["processes"][s["processID"]]["serviceName"]
                tags = {t["key"]: str(t["value"]).lower() for t in s["tags"]}
                if service == query["service"] and all(
                    tags.get(k) == v for k, v in wanted.items()
                ):
                    found.append(trace)
                    break
        return found[: int(query["limit"])]

    async def read_namespaced_pod_log(self, name: str, namespace: str, **kwargs):
        await self._wait()
        self.log_requests.append((name, kwargs))
        return self.logs[name]


def _text(body: str) -> Callable[[], object]:
    async def text() -> str:
        return body

    return text


def span(
    span_id: str,
    service: str,
    operation: str,
    parent: str | None = None,
    error: str | None = None,
    kind: str | None = None,
    peer: str | None = None,
    method: str | None = None,
    started: int = 0,
) -> dict:
    """One Jaeger span. A span with `error` set carries the error tags.

    `started` is seconds before NOW.
    """
    tags = [{"key": "rpc.grpc.status_code", "value": 0}]
    if error is not None:
        tags = [
            {"key": "otel.status_code", "value": "ERROR"},
            {"key": "error", "value": "True"},
            {"key": "otel.status_description", "value": error},
        ]
    for key, value in [
        ("span.kind", kind),
        ("rpc.service", peer),
        ("rpc.method", method),
    ]:
        if value:
            tags.append({"key": key, "value": value})
    refs = [{"refType": "CHILD_OF", "spanID": parent}] if parent else []
    return {
        "spanID": span_id,
        "operationName": operation,
        "references": refs,
        "tags": tags,
        "processID": service,
        "startTime": int((NOW.timestamp() - started) * 1_000_000),
        "logs": [],
    }


def trace(*spans: dict) -> dict:
    """A Jaeger trace. Each span's processID is its service name."""
    processes = {s["processID"]: {"serviceName": s["processID"]} for s in spans}
    return {"traceID": "t", "spans": list(spans), "processes": processes}


def fake_cluster(monkeypatch) -> FakeCluster:
    """Serve the kube tools from a FakeCluster instead of the real test bed."""
    cluster = FakeCluster()

    async def no_kubeconfig() -> None:
        pass

    monkeypatch.setattr(config, "load_kube_config", no_kubeconfig)
    monkeypatch.setattr(kube_tools, "utc_now", lambda: NOW)
    monkeypatch.setattr(
        ApiClient, "call_api", lambda _self, *a, **k: cluster.call_api(*a, **k)
    )
    for api, methods in [
        (AppsV1Api, ["read_namespaced_deployment", "list_namespaced_deployment"]),
        (CoreV1Api, ["list_namespaced_pod", "read_namespaced_pod_log"]),
    ]:
        for method in methods:
            fake = getattr(cluster, method)
            monkeypatch.setattr(
                api, method, lambda _self, *a, _f=fake, **k: _f(*a, **k)
            )
    return cluster
