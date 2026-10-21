"""Small registries for tests, so no test touches the real cluster."""

import asyncio
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

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
        self.prom = FakePrometheus()
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
        if "/services/prometheus:9090/proxy/api/v1/" in path:
            status, body = self.prom.handle(path, dict(kwargs["query_params"]))
            return SimpleNamespace(
                status=status, reason="Service Unavailable", text=_text(body)
            )
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
            for s in trace["spans"]:
                if not int(query["start"]) <= s["startTime"] <= int(query["end"]):
                    continue
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


class FakePrometheus:
    """Prometheus's HTTP API over counter samples held in memory.

    It answers only the query shapes the metrics tools send, and refuses any other
    with the 400 the real server gives for a bad query. It honours the label
    matchers, the range window and the evaluation time, so a tool that asks for
    the wrong service, window or moment gets a different answer.
    Rates are (last - first) / elapsed over the samples in the window, without
    Prometheus's extrapolation.
    """

    SELECTOR = r"(?P<metric>\w+)\{(?P<labels>[^}]*)\}"
    WINDOW = r"\[(?P<window>\d+)m\]"
    SHAPES: ClassVar[dict[str, str]] = {
        "sum": rf"sum\(rate\({SELECTOR}{WINDOW}\)\)",
        "quantile": rf"histogram_quantile\((?P<q>[\d.]+), sum by \(le\) \(rate\({SELECTOR}{WINDOW}\)\)\)",
        "topk": rf"topk\((?P<k>\d+), sum by \(span_name\) \(rate\({SELECTOR}{WINDOW}\)\)\)",
        "count": rf"count\(last_over_time\({SELECTOR}{WINDOW}\)\)",
        "earliest": rf"min\(min_over_time\(timestamp\({SELECTOR}\)\[(?P<window>\d+)m:1m\]\)\)",
    }
    STEP = 15  # seconds between samples

    def __init__(self) -> None:
        self.series: list[tuple[str, dict[str, str], list[tuple[float, float]]]] = []
        self.requests: list[tuple[str, dict]] = []  # (path after v1/, query)
        self.status = 200
        self.body: str | None = None  # replaces the answer

    def counter(
        self,
        metric: str,
        labels: dict[str, str],
        rates: list[tuple[int, float]],
        points: int = 240,
        ended: int = 0,
    ) -> None:
        """A counter sampled every 15 s up to NOW. `rates` is (seconds ago the rate
        starts, per second), oldest first. Samples start 3600 s ago by default and
        stop `ended` seconds ago."""
        samples, value = [], 0.0
        for k in range(points + 1):
            ago = (points - k) * self.STEP
            active = [r for start, r in rates if start > ago]
            value += (active[-1] if active else 0.0) * self.STEP
            if ago >= ended:
                samples.append((NOW.timestamp() - ago, value))
        self.series.append((metric, labels, samples))

    def handle(self, path: str, query: dict) -> tuple[int, str]:
        endpoint = path.split("/proxy/api/v1/")[1]
        self.requests.append((endpoint, query))
        if self.body is not None:
            return self.status, self.body
        if endpoint == "label/service_name/values":
            names = sorted({lab["service_name"] for _, lab, _ in self.series})
            return 200, json.dumps({"status": "success", "data": names})
        try:
            if endpoint == "query":
                at = float(query["time"])
                result = [
                    {"metric": m, "value": [at, v]}
                    for m, v in self._eval(query["query"], at)
                ]
                data = {"resultType": "vector", "result": result}
            else:
                assert endpoint == "query_range", endpoint
                start, end, step = (float(query[k]) for k in ("start", "end", "step"))
                values: dict[tuple, list] = {}
                at = start
                while at <= end:
                    for m, v in self._eval(query["query"], at):
                        values.setdefault(tuple(sorted(m.items())), []).append([at, v])
                    at += step
                result = [{"metric": dict(m), "values": v} for m, v in values.items()]
                data = {"resultType": "matrix", "result": result}
        except ValueError as exc:
            error = {"status": "error", "errorType": "bad_data", "error": str(exc)}
            return 400, json.dumps(error)
        return self.status, json.dumps({"status": "success", "data": data})

    def _eval(self, query: str, at: float) -> list[tuple[dict[str, str], str]]:
        for shape, pattern in self.SHAPES.items():
            if found := re.fullmatch(pattern, query):
                break
        else:
            raise ValueError(f"1:1: parse error: unsupported query {query!r}")
        wanted = re.findall(r'(\w+)(=~|=)"([^"]*)"', found["labels"])
        window = int(found["window"]) * 60
        matching = [
            (labels, samples)
            for metric, labels, samples in self.series
            if metric == found["metric"]
            and all(
                (
                    re.fullmatch(v, labels.get(k, ""))
                    if op == "=~"
                    else labels.get(k) == v
                )
                for k, op, v in wanted
            )
        ]
        if shape == "count":
            seen = [1 for _, s in matching if any(at - window <= t <= at for t, _ in s)]
            return [({}, str(len(seen)))] if seen else []
        if shape == "earliest":
            # A subquery at 1m steps; each step sees the latest sample of the last 5m.
            firsts = []
            for _, samples in matching:
                step = math.ceil((at - window) / 60) * 60
                while step <= at:
                    seen = [t for t, _ in samples if step - 300 < t <= step]
                    if seen:
                        firsts.append(max(seen))
                        break
                    step += 60
            return [({}, str(min(firsts)))] if firsts else []
        rates = []  # (labels, rate per second) of every series that has a rate
        for labels, samples in matching:
            inside = [(t, v) for t, v in samples if at - window <= t <= at]
            if len(inside) >= 2:
                span = inside[-1][0] - inside[0][0]
                rates.append((labels, (inside[-1][1] - inside[0][1]) / span))
        if shape == "sum":
            return [({}, str(sum(r for _, r in rates)))] if rates else []
        if shape == "topk":
            by_name: dict[str, float] = {}
            for labels, r in rates:
                by_name[labels["span_name"]] = by_name.get(labels["span_name"], 0) + r
            top = sorted(by_name.items(), key=lambda item: -item[1])[: int(found["k"])]
            return [({"span_name": n}, str(r)) for n, r in top]
        return [({}, str(self._quantile(float(found["q"]), rates)))] if rates else []

    @staticmethod
    def _quantile(q: float, rates: list[tuple[dict[str, str], float]]) -> float:
        """Prometheus's histogram_quantile: linear inside the bucket the rank falls in."""
        per_le: dict[float, float] = {}
        for labels, r in rates:
            le = float(labels["le"])
            per_le[le] = per_le.get(le, 0) + r
        bounds = sorted(per_le)
        total = per_le[bounds[-1]]
        if total == 0:
            return math.nan
        rank, lower, below = q * total, 0.0, 0.0
        for le in bounds:
            if per_le[le] >= rank:
                if math.isinf(le):
                    return lower
                return lower + (le - lower) * (rank - below) / (per_le[le] - below)
            lower, below = le, per_le[le]
        return math.nan


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
    duration: float = 0,
) -> dict:
    """One Jaeger span. A span with `error` set carries the error tags.

    `started` is seconds before NOW and `duration` is in seconds.
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
        "duration": int(duration * 1_000_000),
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
