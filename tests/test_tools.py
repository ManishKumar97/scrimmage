import asyncio
from typing import Annotated

import pytest
from pydantic import Field

from faultline import kube_tools
from faultline.tools import Registry


def test_schema_comes_from_type_hints_and_docstring():
    registry = Registry()

    @registry.tool
    def get_metrics(
        service: Annotated[str, Field(description="Service name.")],
        minutes: int = 5,
    ) -> str:
        """Get request metrics for one service."""
        return ""

    assert registry.schemas() == [
        {
            "type": "function",
            "function": {
                "name": "get_metrics",
                "description": "Get request metrics for one service.",
                "parameters": {
                    "title": "get_metrics_arguments",
                    "type": "object",
                    "properties": {
                        "service": {
                            "title": "Service",
                            "type": "string",
                            "description": "Service name.",
                        },
                        "minutes": {
                            "title": "Minutes",
                            "type": "integer",
                            "default": 5,
                        },
                    },
                    "required": ["service"],
                },
            },
        }
    ]


def test_the_decorator_returns_the_function_unchanged():
    registry = Registry()

    def ping() -> str:
        """Ping."""
        return "pong"

    assert registry.tool(ping) is ping
    assert ping() == "pong"


def test_a_parameter_without_a_type_hint_is_refused():
    registry = Registry()

    def untyped(service):
        """No hint."""

    with pytest.raises(TypeError, match=r"untyped\(service\) needs a type hint"):
        registry.tool(untyped)


def call(registry: Registry, name: str, arguments: str) -> tuple[str, bool]:
    return asyncio.run(registry.call(name, arguments))


def test_defaults_apply_and_types_are_checked():
    registry = Registry()

    @registry.tool
    def tail(service: str, lines: int = 100) -> str:
        """Tail."""
        return f"{service}:{lines}"

    assert call(registry, "tail", '{"service": "cart"}') == ("cart:100", True)
    assert call(registry, "tail", '{"service": "cart", "lines": "7"}') == (
        "cart:7",
        True,
    )
    output, ok = call(registry, "tail", '{"service": "cart", "lines": "many"}')
    assert not ok
    assert "lines: Input should be a valid integer" in output


def test_the_test_bed_registry_has_both_kubectl_tools():
    assert list(kube_tools.registry.tools) == ["get_logs", "list_services"]
    list_services = kube_tools.registry.tools["list_services"].arguments
    assert list_services.model_json_schema()["properties"] == {}


def test_async_and_sync_tools_are_both_called():
    registry = Registry()

    @registry.tool
    async def ping(name: str) -> str:
        """Ping."""
        await asyncio.sleep(0)
        return f"pong {name}"

    @registry.tool
    def echo(name: str) -> str:
        """Echo."""
        return name

    assert call(registry, "ping", '{"name": "a"}') == ("pong a", True)
    assert call(registry, "echo", '{"name": "b"}') == ("b", True)
    ping_schema = registry.tools["ping"].arguments.model_json_schema()
    assert ping_schema["required"] == ["name"]
