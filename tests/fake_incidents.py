"""A stand-in for the Kubernetes API in front of flagd-ui, Prometheus and the pod list."""

import json
from types import SimpleNamespace
from typing import cast

from kubernetes_asyncio.client import ApiClient, ApiException

FLAG_FILE = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        "cartFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "paymentFailure": {
            "state": "ENABLED",
            "variants": {"100%": 1, "50%": 0.5, "off": 0},
            "defaultVariant": "off",
        },
    },
}


class FakeFlagdUi:
    """flagd-ui's two routes over one flag file, with the shapes read from its code."""

    BASE = "/api/v1/namespaces/astronomy-shop/services/flagd:4000/proxy/feature/api/"

    def __init__(self) -> None:
        self.file = json.loads(json.dumps(FLAG_FILE))
        self.requests: list[tuple[str, str]] = []
        self.status = 200
        self.ignore_writes = False
        self.body: str | None = None

    def handle(self, path: str, method: str, kwargs: dict) -> SimpleNamespace:
        route = path.removeprefix(self.BASE)
        self.requests.append((method, route))
        if self.status >= 400:
            return reply(self.status, "{}")
        if route == "read-file" and method == "GET":
            # The real route reads the file named in the query, default demo.flagd.json.
            assert dict(kwargs["query_params"]) == {"file_name": "demo.flagd.json"}
            return reply(200, self.body or json.dumps(self.file))
        if route == "write-to-file" and method == "POST":
            assert kwargs["header_params"]["Content-Type"] == "application/json"
            if not self.ignore_writes:
                self.file = json.loads(json.dumps(kwargs["body"]["data"]))
            return reply(200, json.dumps({"message": "File written successfully"}))
        return reply(404, "not found")


def reply(status: int, text: str) -> SimpleNamespace:
    async def body() -> str:
        return text

    return SimpleNamespace(status=status, reason="reason", text=body)


class FakeApi:
    """Routes call_api to the fake services behind the Kubernetes service proxy."""

    def __init__(self) -> None:
        self.flagd = FakeFlagdUi()
        # Exact PromQL text -> result vector. Any other query gets Prometheus's 400.
        self.queries: dict[str, list[dict]] = {}
        self.raise_on_call: Exception | None = None

    @property
    def client(self) -> ApiClient:
        """This fake, typed as the client the code under test asks for."""
        return cast(ApiClient, self)

    async def call_api(self, path: str, method: str, **kwargs):
        if self.raise_on_call:
            raise self.raise_on_call
        if path.startswith(FakeFlagdUi.BASE):
            return self.flagd.handle(path, method, kwargs)
        assert "/services/prometheus:9090/proxy/api/v1/query" in path, path
        query = dict(kwargs["query_params"])["query"]
        if query not in self.queries:
            return reply(400, json.dumps({"status": "error", "error": "bad query"}))
        body = {"status": "success", "data": {"result": self.queries[query]}}
        return reply(200, json.dumps(body))


def not_found() -> ApiException:
    return ApiException(status=404, reason="Not Found")


def vector(value: float, **labels: str) -> dict:
    return {"metric": labels, "value": [0, str(value)]}


def pod(name: str, phase: str = "Running", ready: tuple[bool, ...] = (True,)):
    statuses = [SimpleNamespace(ready=r) for r in ready]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase=phase, container_statuses=statuses),
    )
