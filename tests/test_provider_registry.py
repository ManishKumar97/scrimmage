"""The registry resolves a request to a provider and an op, per call, by host, path and method.

A stub second provider stands in for the one that comes later: a host listed once for each
provider is the case the lookup is built for. The guards at the end keep the product's modules
from reaching a provider except through the registry, and keep the places that name the
providers in step."""

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import cast

import pytest

import scrimmage
from scrimmage import labels
from scrimmage import plan as plan_module
from scrimmage.config import ModelHost
from scrimmage.providers.base import ModelProvider
from scrimmage.providers.registry import (
    ProviderRegistry,
    built_in,
    builtin_providers,
    registry_for,
)
from test_provider_contract import CASES

SRC = Path(scrimmage.__file__).parent
ROOT = Path(__file__).parents[1]


class Stub:
    """A second provider that speaks POST .../v1/messages, as Anthropic does."""

    name = "stub"
    hosts = ("api.stub.test",)
    free_ops = ("input_tokens",)

    def op_for(self, path: str, method: str) -> str | None:
        clean = path.split("?", 1)[0].rstrip("/")
        if method != "POST" or "/threads/" in clean:
            return None
        if clean.endswith("/v1/messages/count_tokens"):
            return "input_tokens"
        return "chat" if clean.endswith("/v1/messages") else None


def with_stub(*listed: tuple[str, str]) -> ProviderRegistry:
    return ProviderRegistry((*builtin_providers(), cast(ModelProvider, Stub())), listed)


def two() -> ProviderRegistry:
    return with_stub(("gw.test", "openai"))


def test_a_built_in_host_resolves_to_its_provider() -> None:
    found = two().resolve("api.openai.com", "/v1/chat/completions", "POST")
    assert found is not None
    provider, op = found
    assert (provider.name, op) == ("openai", "chat")
    found = two().resolve("api.stub.test", "/v1/messages", "POST")
    assert found is not None
    assert (found[0].name, found[1]) == ("stub", "chat")


def test_a_host_listed_once_for_each_provider_is_picked_by_its_path() -> None:
    both = with_stub(("gw.test", "openai"), ("gw.test", "stub"))
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
    flipped = with_stub(("gw.test", "stub"), ("gw.test", "openai"))
    chat = flipped.resolve("gw.test", "/v1/chat/completions", "POST")
    assert chat is not None
    assert chat[0].name == "openai"


def test_openais_threads_messages_path_is_no_call_on_a_host_for_both() -> None:
    both = with_stub(("gw.test", "openai"), ("gw.test", "stub"))
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
    both = with_stub(("api.openai.com", "stub"))
    assert [p.name for p in both.candidates("api.openai.com")] == ["stub", "openai"]


def test_a_host_listed_twice_for_one_provider_has_it_once() -> None:
    twice = with_stub(("gw.test", "openai"), ("gw.test", "openai"))
    assert [p.name for p in twice.candidates("gw.test")] == ["openai"]


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
        ProviderRegistry(builtin_providers(), (("gw.test", "nope"),))
    with pytest.raises(ValueError, match="unknown provider"):
        built_in().get("nope")


def test_built_in_hosts() -> None:
    registry = two()
    assert registry.is_built_in_host("api.openai.com")
    assert registry.is_built_in_host("api.stub.test")
    assert not registry.is_built_in_host("gw.test")


def test_a_registry_is_built_once_for_the_same_hosts() -> None:
    hosts = (("gw.test", "openai"),)
    assert registry_for(hosts) is registry_for(hosts)
    assert registry_for(hosts) is not registry_for()
    plan = plan_module.Plan(
        "r", "t", "row", (), (), (), (), 1.0, model_hosts=(ModelHost("gw.test", "openai"),)
    )
    assert plan.registry is registry_for(hosts)
    assert plan.registry.candidates("gw.test")[0].name == "openai"


def test_a_plan_keeps_its_registry_once() -> None:
    """The registry is carried on the plan: asking again is the same object, with no lookup."""
    plan = plan_module.Plan("r", "t", "row", (), (), (), (), 1.0)
    assert plan.registry is plan.registry
    assert "registry" in vars(plan)


def test_a_plan_that_names_an_unknown_provider_is_a_bad_plan() -> None:
    doc = plan_module.Plan("r", "t", "row", (), (), (), (), 1.0).to_json()
    doc["model_hosts"] = [{"host": "gw.test", "provider": "nope"}]
    with pytest.raises(ValueError, match="unknown provider"):
        plan_module.Plan.from_json(doc)


# The provider names live in four places that nothing else links: the registry, the labels, and
# the enum of each of the two schemas. A provider half registered passes until a config names it.


def provider_enums(node: object) -> list[list[str]]:
    """Every enum that is the value of a "provider" property, anywhere in a schema."""
    found: list[list[str]] = []
    if isinstance(node, dict):
        for key, value in cast("dict[str, object]", node).items():
            if key == "provider" and isinstance(value, dict) and "enum" in value:
                found.append(cast("list[str]", value["enum"]))
            else:
                found += provider_enums(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            found += provider_enums(item)
    return found


def test_the_provider_names_agree_in_the_registry_the_labels_and_both_schemas() -> None:
    names = tuple(p.name for p in builtin_providers())
    assert names == labels.PROVIDERS, "labels.PROVIDERS and the registry name other providers"
    for schema in ("plan.v1.json", "scrimmage-toml.v1.json"):
        enums = provider_enums(json.loads((SRC / "schemas" / schema).read_text()))
        assert enums, f"{schema} no longer has a provider enum"
        for enum in enums:
            assert tuple(enum) == names, f"{schema} lists {enum}, the registry {list(names)}"


@pytest.mark.parametrize("name", [p.name for p in builtin_providers()])
def test_a_registered_provider_is_one_a_config_can_name(name: str) -> None:
    labels.check_model(f"model:{name}", "test")
    labels.check_model(f"model:{name}/embeddings", "test")
    ModelHost("gw.test", name)
    registry_for((("gw.test", name),))


LABEL_WORDS = {"model", "embeddings", "moderations", "input_tokens"}


@pytest.mark.parametrize("schema", ["plan.v1.json", "scrimmage-toml.v1.json"])
def test_the_model_label_pattern_of_each_schema_names_the_registered_providers(
    schema: str,
) -> None:
    """A label such as model:openai/embeddings is matched by a pattern that lists the providers;
    one left behind passes until a config uses the new provider's label."""
    pattern = json.loads((SRC / "schemas" / schema).read_text())["$defs"]["model_label"]["pattern"]
    names = [p.name for p in builtin_providers()]
    for name in names:
        assert re.search(pattern, f"model:{name}"), f"{schema} does not take model:{name}"
        assert re.search(pattern, f"model:{name}/embeddings"), name
    assert re.search(pattern, "model")
    assert re.search(pattern, "model:*/embeddings")
    assert not re.search(pattern, "model:nonesuch")
    assert not re.search(pattern, "model:*/nonesuch")
    listed = set(re.findall(r"[a-z][a-z0-9_-]+", pattern)) - LABEL_WORDS
    assert listed == set(names), f"{schema} names {sorted(listed)}, the registry {sorted(names)}"


def test_the_agreement_check_sees_a_provider_left_out_of_a_schema() -> None:
    """The check above is not vacuous: an enum short of a provider is not the registry's."""
    schema = {"a": {"properties": {"provider": {"enum": ["openai"]}}}}
    assert provider_enums(schema) == [["openai"]]
    assert tuple(provider_enums(schema)[0]) != (*(p.name for p in builtin_providers()), "other")


# The guard: no module outside the providers package reaches a provider except through the
# registry's own names, and none decides by the wire form of an API. A provider is a module and
# a registry entry; nothing else changes.

REGISTRY_NAMES = {"ProviderRegistry", "registry_for", "built_in"}  # what a module may import
# A report shows what a model said, so it reads the keys of a recorded reply. That is display,
# not a decision, and it is the one module allowed to name a provider's keys.
READS_REPLIES_TO_SHOW = {"show.py"}
# The APIs of the providers that are not registered yet, named here so the guard bites before
# they are. Each provider that is registered adds its own through its contract case.
COMING_PATHS = {"/messages", "/v1/messages", "/messages/count_tokens"}
COMING_KEYS = {"stop_reason", "content_block", "input_json_delta", "tool_use", "tool_result"}


def path_forms(path: str) -> set[str]:
    """The ways a path names its API: its last one and two segments, with and without the slash."""
    parts = path.split("?")[0].strip("/").split("/")
    tails = {"/".join(parts[-k:]) for k in (1, 2) if len(parts) >= k}
    return tails | {"/" + tail for tail in tails}


def api_literals() -> set[str]:
    """The paths that name an API of any registered provider, from its contract case, and the
    paths of those to come. Only a chat op is an API of its own: embeddings, moderations and
    input_tokens are the same words for every provider."""
    found = set(COMING_PATHS)
    for case in CASES.values():
        for _, path, op in case.ops:
            if op == "chat":
                found |= path_forms(path)
    return found


def wire_keys() -> set[str]:
    """The keys of a registered provider's requests and replies that name its shape."""
    return {key for case in CASES.values() for key in case.wire_keys} | COMING_KEYS


def product_modules() -> list[Path]:
    """Every module of the product that is not a provider: all of scrimmage but its providers
    package and its provider_*.py table modules."""
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if "providers" not in path.relative_to(SRC).parts[:-1]
        and not path.name.startswith("provider_")
    )


def table_modules() -> set[str]:
    return {path.stem for path in SRC.glob("provider_*.py")}


def provider_classes() -> set[str]:
    return {type(p).__name__ for p in builtin_providers()}


def defined_in(path: Path) -> set[str]:
    """The top-level names a module defines."""
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def shape_names() -> set[str]:
    """The names of a provider's reply shapes, from its *_changes.py modules, that no product
    module defines for itself: a product module that uses one is calling into a shape."""
    shapes: set[str] = set()
    for path in (SRC / "providers" / "impl").glob("*_changes.py"):
        shapes |= defined_in(path)
    for path in product_modules():
        shapes -= defined_in(path)
    return {name for name in shapes if not name.startswith("_")}


def reaches_a_provider(module: str) -> bool:
    """Whether this module name is a provider's own: a table module or anything in providers
    beside the interface and the registry."""
    parts = module.split(".")
    if parts[:1] != ["scrimmage"] or len(parts) < 2:
        return False
    if parts[1] in table_modules():
        return True
    return parts[1] == "providers" and len(parts) > 2 and parts[2] not in ("base", "registry")


def problems_in(source: str, *, reads_replies: bool = False) -> list[str]:
    """What in this source reaches a provider, or decides by the wire form of an API of one."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    classes, tables, shapes = provider_classes(), table_modules(), shape_names()
    literals, keys = api_literals(), wire_keys()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"imports {a.name}" for a in node.names if reaches_a_provider(a.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                found.append(f"imports {'.' * node.level}{base}, which no guard can read")
            if reaches_a_provider(base):
                found.append(f"imports from {base}")
            for alias in node.names:
                if reaches_a_provider(f"{base}.{alias.name}"):
                    found.append(f"imports {base}.{alias.name}")
                if base == "scrimmage.providers.registry" and alias.name not in REGISTRY_NAMES:
                    found.append(f"imports {alias.name} from the registry")
                if alias.name in classes:
                    found.append(f"imports {alias.name}, a provider's class")
                if alias.name in shapes:
                    found.append(f"imports {alias.name}, one of a provider's reply shapes")
        elif isinstance(node, ast.Name) and (node.id in classes or node.id in tables):
            found.append(f"names {node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr in classes or node.attr in tables):
            found.append(f"names {node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in shapes:
            found.append(f"calls into {node.attr}, one of a provider's reply shapes")
        elif isinstance(node, ast.Name) and node.id in shapes:
            found.append(f"calls into {node.id}, one of a provider's reply shapes")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("endswith", "startswith")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("/")
        ):
            found.append(f"matches a path, {node.args[0].value!r}, which is the provider's to know")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if node.value in literals:
                found.append(f"names the API path {node.value!r}")
            elif node.value in keys and not reads_replies:
                found.append(f"names the wire key {node.value!r}")
    return found


@pytest.mark.parametrize("path", product_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_product_module_reaches_a_provider_or_decides_by_one_of_its_apis(path: Path) -> None:
    """The gate, the changes, the run's state, the config, init and every other module know a
    provider only through the interface and the registry."""
    bad = problems_in(path.read_text(), reads_replies=path.name in READS_REPLIES_TO_SHOW)
    assert not bad, f"{path.relative_to(SRC)}: {bad}"


def test_the_literals_the_guard_looks_for_come_from_every_registered_provider() -> None:
    assert set(CASES) == {p.name for p in builtin_providers()}
    literals = api_literals()
    assert {"/responses", "responses", "/chat/completions", "chat/completions"} <= literals
    assert "/messages" in literals
    assert not literals & {"embeddings", "moderations", "input_tokens", "/embeddings"}
    assert {"choices", "previous_response_id", "stop_reason"} <= wire_keys()
    names = shape_names()
    assert {"chat_kind", "model_change", "CHAT_CHANGES", "RESPONSES_CHANGES"} <= names
    assert "change_chat" in names


GATE = "def f(x):\n    return x\n"


@pytest.mark.parametrize(
    ("added", "says"),
    [
        ("from scrimmage import provider_openai\n", "imports scrimmage.provider_openai"),
        ("import scrimmage.provider_openai\n", "imports scrimmage.provider_openai"),
        ("from scrimmage.provider_openai import usage\n", "imports from scrimmage.provider_openai"),
        ("from scrimmage.providers.impl import openai\n", "imports scrimmage.providers.impl"),
        (
            "from scrimmage.providers.impl.openai import OpenAI\n",
            "imports from scrimmage.providers",
        ),
        (
            "from scrimmage.providers.impl import openai_changes\n",
            "imports scrimmage.providers.impl.openai_changes",
        ),
        ("from scrimmage.providers.registry import OpenAI\n", "imports OpenAI from the registry"),
        ("from scrimmage.providers.registry import PROVIDERS\n", "imports PROVIDERS from the reg"),
        (
            "from scrimmage.providers.openai import OpenAI\n",
            "imports from scrimmage.providers.open",
        ),
        (
            "from scrimmage.providers.anthropic import Claude\n",
            "imports from scrimmage.providers.anth",
        ),
        ("from .provider_openai import usage\n", "which no guard can read"),
        ("def g(op):\n    return op == 'responses'\n", "names the API path 'responses'"),
        ("def g(path):\n    return path.endswith('/responses')\n", "'/responses'"),
        ("def g(path):\n    return path.endswith('/chat/completions')\n", "'/chat/completions'"),
        ("def g(x):\n    return x.provider_openai.usage\n", "names provider_openai"),
        ("def g():\n    return OpenAI()\n", "names OpenAI"),
        # The reviewer's S5: the gate decides by another API's path, or by a key of its replies.
        ("def g(path):\n    return path.endswith('/messages')\n", "names the API path '/messages'"),
        ("def g(path):\n    return path.endswith('/v1/other')\n", "matches a path, '/v1/other'"),
        ("def g(path):\n    return path.startswith('/x')\n", "matches a path, '/x'"),
        ("def g(fields):\n    return fields.get('choices')\n", "names the wire key 'choices'"),
        ("def g(fields):\n    return fields['stop_reason']\n", "names the wire key 'stop_reason'"),
        # The reviewer's S6: the gate calls a provider's reply shape itself.
        (
            "def g(x):\n    return changes.chat_kind(1, 'chat', [], 'garble')\n",
            "calls into chat_kind",
        ),
        (
            "def g(x):\n    return changes.model_change('garble', 'chat')\n",
            "calls into model_change",
        ),
        ("from scrimmage.changes import chat_kind\n", "imports chat_kind, one of"),
        ("def g(x):\n    return CHAT_CHANGES[x]\n", "CHAT_CHANGES"),
    ],
)
def test_the_guard_catches_each_way_a_module_could_reach_a_provider(added: str, says: str) -> None:
    bad = problems_in(GATE + added)
    assert any(says in problem for problem in bad), bad


@pytest.mark.parametrize(
    "fine",
    [
        "from scrimmage.providers.base import ModelProvider, ServerChains\n",
        "from scrimmage.providers.registry import ProviderRegistry, built_in, registry_for\n",
        "from scrimmage import changes, state\n",
        "def g(op):\n    '''A responses call.'''\n    return op == 'chat'\n",
        "def g(provider):\n    return provider.name\n",
        "def g(fields):\n    return fields.get('stream') is True and fields.get('model')\n",
        "def g(x):\n    return changes.change_text(x, 1)\n",
        "def g(x):\n    return x.endswith('.py')\n",
    ],
)
def test_the_guard_leaves_alone_what_a_module_may_use(fine: str) -> None:
    assert problems_in(GATE + fine) == []


def test_the_one_module_that_shows_replies_may_name_their_keys_and_nothing_more() -> None:
    shows = "def g(r):\n    return r.get('choices')\n"
    assert problems_in(shows, reads_replies=True) == []
    assert problems_in(shows)
    assert problems_in(shows + "from scrimmage.provider_openai import x\n", reads_replies=True)
    assert problems_in(
        shows + "def h(p):\n    return p.endswith('/responses')\n", reads_replies=True
    )


def test_every_provider_table_is_listed_in_the_import_contract() -> None:
    """A provider_*.py file no one listed would be reachable from the gate unchecked."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    (contract,) = [c for c in contracts if c["name"].startswith("Only the providers package")]
    forbidden = set(contract["forbidden_modules"])
    ignored = set(contract["ignore_imports"])
    assert "scrimmage.providers.impl" in forbidden
    assert "scrimmage.providers.registry -> scrimmage.providers.impl" in ignored
    for table in table_modules():
        assert f"scrimmage.{table}" in forbidden, f"{table} is not forbidden to the product"
        assert f"scrimmage.providers.** -> scrimmage.{table}" in ignored, f"{table} cannot load"
    listed = {re.sub(r"^scrimmage\.", "", m) for m in forbidden} - {"providers.impl"}
    assert listed == table_modules(), "the contract names a table module that does not exist"
    assert contract["source_modules"] == ["scrimmage.*"], "the contract guards every module"
    assert not contract.get("allow_indirect_imports", False)
