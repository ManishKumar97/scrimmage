"""The registry resolves a request to a provider and an op, per call, by host, path and method.

A stub second provider stands in for the one that comes later: a host listed once for each
provider is the case the lookup is built for."""

import ast
import re
from pathlib import Path
from typing import cast

import pytest

from scrimmage import plan as plan_module
from scrimmage.config import ModelHost
from scrimmage.providers.base import ModelProvider
from scrimmage.providers.registry import PROVIDERS, ProviderRegistry, built_in, registry_for

SRC = Path(__file__).parents[1] / "packages" / "scrimmage" / "src" / "scrimmage"
GATE_MODULES = ("http_gate", "changes", "state", "config", "init")


class Stub:
    """A second provider that speaks POST .../v1/messages, as Anthropic does."""

    name = "stub"
    hosts = ("api.stub.test",)
    free_ops = ("input_tokens",)
    apis = ("messages",)

    def op_for(self, path: str, method: str) -> str | None:
        clean = path.split("?", 1)[0].rstrip("/")
        if method != "POST" or "/threads/" in clean:
            return None
        if clean.endswith("/v1/messages/count_tokens"):
            return "input_tokens"
        return "chat" if clean.endswith("/v1/messages") else None


def two() -> ProviderRegistry:
    return ProviderRegistry((*PROVIDERS, cast(ModelProvider, Stub())), (("gw.test", "openai"),))


def test_a_built_in_host_resolves_to_its_provider() -> None:
    found = two().resolve("api.openai.com", "/v1/chat/completions", "POST")
    assert found is not None
    provider, op = found
    assert (provider.name, op) == ("openai", "chat")
    found = two().resolve("api.stub.test", "/v1/messages", "POST")
    assert found is not None
    assert (found[0].name, found[1]) == ("stub", "chat")


def test_a_host_listed_once_for_each_provider_is_picked_by_its_path() -> None:
    both = ProviderRegistry(
        (*PROVIDERS, cast(ModelProvider, Stub())), (("gw.test", "openai"), ("gw.test", "stub"))
    )
    chat = both.resolve("gw.test", "/v1/chat/completions", "POST")
    messages = both.resolve("gw.test", "/v1/messages", "POST")
    count = both.resolve("gw.test", "/v1/messages/count_tokens", "POST")
    assert chat is not None
    assert messages is not None
    assert count is not None
    assert (chat[0].name, chat[1]) == ("openai", "chat")
    assert (messages[0].name, messages[1]) == ("stub", "chat")
    assert (count[0].name, count[1]) == ("stub", "input_tokens")


def test_the_order_a_host_is_listed_in_does_not_change_which_provider_a_path_picks() -> None:
    flipped = ProviderRegistry(
        (*PROVIDERS, cast(ModelProvider, Stub())), (("gw.test", "stub"), ("gw.test", "openai"))
    )
    chat = flipped.resolve("gw.test", "/v1/chat/completions", "POST")
    assert chat is not None
    assert chat[0].name == "openai"


def test_openais_threads_messages_path_is_no_call_on_a_host_for_both() -> None:
    both = ProviderRegistry(
        (*PROVIDERS, cast(ModelProvider, Stub())), (("gw.test", "openai"), ("gw.test", "stub"))
    )
    assert both.resolve("gw.test", "/v1/threads/t_1/messages", "POST") is None


def test_a_host_no_one_owns_resolves_to_nothing_and_has_no_candidates() -> None:
    registry = two()
    assert registry.candidates("example.com") == ()
    assert registry.resolve("example.com", "/v1/chat/completions", "POST") is None


def test_a_listed_host_with_a_path_that_is_not_a_model_call_keeps_its_candidates() -> None:
    registry = two()
    assert [p.name for p in registry.candidates("gw.test")] == ["openai"]
    assert registry.resolve("gw.test", "/v1/models", "POST") is None
    assert registry.resolve("gw.test", "/v1/chat/completions", "GET") is None


def test_a_built_in_host_is_a_candidate_after_the_providers_listed_for_it() -> None:
    both = ProviderRegistry(
        (*PROVIDERS, cast(ModelProvider, Stub())), (("api.openai.com", "stub"),)
    )
    assert [p.name for p in both.candidates("api.openai.com")] == ["stub", "openai"]


def test_the_probe_tries_every_provider() -> None:
    registry = two()
    found = registry.probe("/v1/messages", "POST")
    assert found is not None
    assert found[0].name == "stub"
    assert registry.probe("/v1/models", "POST") is None
    assert registry.recognizes("/v1/chat/completions", "POST")
    assert not registry.recognizes("/v1/chat/completions", "GET")


def test_a_host_names_a_provider_the_registry_has() -> None:
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        ProviderRegistry(PROVIDERS, (("gw.test", "nope"),))
    with pytest.raises(ValueError, match="unknown provider"):
        built_in().get("nope")


def test_built_in_hosts_and_apis() -> None:
    registry = two()
    assert registry.is_built_in_host("api.openai.com")
    assert registry.is_built_in_host("api.stub.test")
    assert not registry.is_built_in_host("gw.test")
    found = registry.for_api("messages")
    assert found is not None
    assert found.name == "stub"
    assert registry.for_api("nothing") is None


def test_a_registry_is_built_once_for_the_same_hosts() -> None:
    hosts = (("gw.test", "openai"),)
    assert registry_for(hosts) is registry_for(hosts)
    assert registry_for(hosts) is not registry_for()
    plan = plan_module.Plan(
        "r", "t", "row", (), (), (), (), 1.0, model_hosts=(ModelHost("gw.test", "openai"),)
    )
    assert plan.registry is registry_for(hosts)
    assert plan.registry.candidates("gw.test")[0].name == "openai"


def test_a_plan_that_names_an_unknown_provider_is_a_bad_plan() -> None:
    doc = plan_module.Plan("r", "t", "row", (), (), (), (), 1.0).to_json()
    doc["model_hosts"] = [{"host": "gw.test", "provider": "nope"}]
    with pytest.raises(ValueError, match="unknown provider"):
        plan_module.Plan.from_json(doc)


def imported(module: str) -> list[str]:
    """Every module name this source file imports, and every name it imports from one."""
    names: list[str] = []
    for node in ast.walk(ast.parse((SRC / f"{module}.py").read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names += [f"{base}.{alias.name}" for alias in node.names]
    return names


PROVIDER_MODULE = re.compile(r"(^|\.)provider_\w+$|(^|\.)providers\.(?!base$|registry$)\w+$")
PROVIDER_WORD = re.compile(r"\bprovider_(openai|anthropic)\b|\bopenai_table\b")


@pytest.mark.parametrize("module", GATE_MODULES)
def test_the_gate_never_imports_or_names_a_provider_module(module: str) -> None:
    """The gate, the changes, the run's state, the config and init know a provider only through
    the interface and the registry. A new provider is a module and a registry entry."""
    bad = [name for name in imported(module) if PROVIDER_MODULE.search(name)]
    assert not bad, f"{module}.py imports {bad}"
    named = PROVIDER_WORD.findall((SRC / f"{module}.py").read_text())
    assert not named, f"{module}.py names {named}"
