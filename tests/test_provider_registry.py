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
# The APIs of the providers that are not registered yet, named here so the guard bites before
# they are. Each provider that is registered adds its own through its contract case.
COMING_PATHS = {"/messages", "/v1/messages", "/messages/count_tokens"}
COMING_KEYS = {"stop_reason", "content_block", "input_json_delta", "tool_use", "tool_result"}
COMING_PROVIDERS = {"anthropic"}  # the names of providers not registered yet
# The modules that may name a provider: the vocabulary of the labels, and init's list of what an
# agent imports.
NAMES_PROVIDERS = {"labels.py", "init.py"}
# The three product regexes that start with a slash and are no provider's path: the control
# plane's own API, the slash runs of a credential's path, and the shape of a web path.
OWN_PATTERNS = {
    ("control.py", r"/v1/plans/([A-Za-z0-9_-]{1,64})"),
    ("credentials.py", r"/{2,}"),
    ("labels.py", r"/(?:(?![?#])[\x21-\x7e])*"),
}
# The modules that get parsed wire data only from a provider, so they never parse JSON
# themselves: json.loads returns Any, which the opaque types cannot see through. The report
# hands a recorded reply's bytes to its provider. Its two other readers parse the records the
# run wrote, whose keys are Scrimmage's own, so those two functions are the only allowed sites.
NO_JSON_PARSING = {"http_gate.py", "state.py", "show.py"}
JSON_SITES = {("show.py", "summary_of"), ("show.py", "timeline")}
JSON_LIBRARIES = {"json", "orjson", "ujson", "simplejson"}
# Every way to read JSON the libraries offer: the functions, and the decoder class, whose
# .decode and .raw_decode read a string.
JSON_READERS = {"loads", "load", "JSONDecoder", "decoder", "decode", "raw_decode"}
RE_LIBRARIES = {"re", "regex"}
# No module outside the providers may build or look up a provider's module by name.
MODULE_NAMES = ("scrimmage.provider_", "providers.impl")


def path_forms(path: str) -> set[str]:
    """The ways a path names its API: its last one and two segments, with and without the slash."""
    parts = path.split("?")[0].strip("/").split("/")
    tails = {"/".join(parts[-k:]) for k in (1, 2) if len(parts) >= k}
    return tails | {"/" + tail for tail in tails}


def api_literals() -> set[str]:
    """The paths that name an API of any registered provider, from its contract case, and the
    paths of those to come. Only a chat op is an API of its own: embeddings, moderations and
    input_tokens are the same words for every provider."""
    found = set(COMING_PATHS).union(*(path_forms(path) for path in COMING_PATHS))
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


def imported(tree: ast.AST, libraries: set[str]) -> tuple[set[str], set[str], bool]:
    """How a tree names these libraries, in every import form: the names bound to a library
    module (import re as r), the names bound to one of its members (from re import search as
    s), and whether it star-imports one."""
    modules: set[str] = set()
    members: set[str] = set()
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in libraries:
                    modules.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in libraries:
            for a in node.names:
                star = star or a.name == "*"
                members.add(a.asname or a.name)
    return modules, members, star


def constants(tree: ast.AST) -> dict[str, str]:
    """The names a tree sets once to a string, so a pattern held in a constant is seen."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names, value = [node.target.id], node.value
        else:
            continue
        text = text_of(value, found) if value is not None else None
        if text is not None:
            for name in names:
                found[name] = text
    return found


def text_of(node: ast.expr, named: dict[str, str]) -> str | None:
    """The string an expression holds when it is a literal, a constant's name, or a sum or an
    f-string of those; the parts it cannot know are left out."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return named.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = text_of(node.left, named), text_of(node.right, named)
        return None if left is None and right is None else (left or "") + (right or "")
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def paths_in_patterns(tree: ast.AST, module: str) -> list[str]:
    """The re and regex calls and the match cases of this tree whose pattern names an API's
    path, anywhere in it, or starts with a slash: a path matched by its form, which is the
    provider's to know. The libraries are seen under every import form, and a pattern held in
    a constant is read."""
    modules, members, star = imported(tree, RE_LIBRARIES)
    named = constants(tree)
    literals = api_literals()
    found: list[str] = []
    if star:
        found.append("imports * from a regex library, which no guard can read")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            ):
                call = f"{func.value.id}.{func.attr}"
            elif isinstance(func, ast.Name) and func.id in members:
                call = func.id
            else:
                continue
            argument = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "pattern":
                    argument = keyword.value
            text = None if argument is None else text_of(argument, named)
            if text is None or (module, text) in OWN_PATTERNS:
                continue
            if text.startswith("/") or any(lit in text for lit in literals):
                found.append(f"matches a path with {call}, {text!r}")
        elif isinstance(node, ast.match_case):
            found += [
                f"matches a path in a case, {leaf.value!r}"
                for leaf in ast.walk(node.pattern)
                if isinstance(leaf, ast.Constant)
                and isinstance(leaf.value, str)
                and leaf.value.startswith("/")
            ]
    return found


def enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """The name of the function each node sits in, by the node's id."""
    names: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                names[id(node)] = fn.name
    return names


def parses_json(tree: ast.AST, module: str) -> list[str]:
    """The uses and imports of a JSON reader, in a module that gets wire data from a provider:
    loads and load, the decoder class and its decode and raw_decode, under any import form."""
    if module not in NO_JSON_PARSING:
        return []
    modules, _, _ = imported(tree, JSON_LIBRARIES)
    inside = enclosing_functions(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        site = (module, inside.get(id(node), ""))
        if site in JSON_SITES:
            continue
        if isinstance(node, ast.Attribute) and (
            node.attr == "raw_decode"
            or (
                node.attr in JSON_READERS
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
            )
        ):
            owner = node.value.id if isinstance(node.value, ast.Name) else "a decoder"
            found.append(f"parses JSON with {owner}.{node.attr}")
        elif (
            isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in JSON_LIBRARIES
        ):
            found += [
                f"imports {node.module}.{a.name}, a JSON reader"
                for a in node.names
                if a.name in JSON_READERS or node.module != (node.module or "").split(".")[0]
            ]
        elif isinstance(node, ast.Import):
            found += [
                f"imports {a.name}, a JSON reader"
                for a in node.names
                if a.name.split(".")[0] in JSON_LIBRARIES and "." in a.name
            ]
    return found


OPAQUE_TYPES = {"Fields", "Reply"}
# What reads an object without the type checker's say: it takes any object, so pyright passes
# Fields to it, and at run time it sees the class, not the dict inside.
PROBES = {"isinstance", "getattr", "hasattr", "vars", "repr", "str", "ascii", "format", "cast"}


def opaque_names(tree: ast.AST) -> set[str]:
    """The names in a module that hold an opaque wire value: parameters and variables typed
    Fields or Reply, from the provider interface, and what request_fields and parse_reply
    return. A module that imports neither type holds none."""
    types: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "scrimmage.providers.base":
            types |= {a.asname or a.name for a in node.names if a.name in OPAQUE_TYPES}
    if not types:
        return set()

    def typed(annotation: ast.expr | None) -> bool:
        return annotation is not None and any(
            (isinstance(n, ast.Name) and n.id in types)
            or (isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in types)
            for n in ast.walk(annotation)
        )

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and typed(node.annotation):
            names.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if typed(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            made = ast.unparse(node.value)
            if any(word in made for word in ("request_fields(", "parse_reply(", "NO_FIELDS")):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def probes_opaque(tree: ast.AST) -> list[str]:
    """The places a module reads an opaque wire value by a way the type checker lets through:
    isinstance, getattr, hasattr, vars, repr, str, format, a cast, an f-string, or a variable
    typed Any, on a name that holds a Fields or a Reply."""
    held = opaque_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in PROBES:
                checked = node.args[1:2] if node.func.id == "cast" else node.args[:1]
                found += [
                    f"probes the opaque {a.id} with {node.func.id}"
                    for a in checked
                    if isinstance(a, ast.Name) and a.id in held
                ]
        elif isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name):
            if node.value.id in held:
                found.append(f"formats the opaque {node.value.id}")
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Name)
            and node.value.id in held
            and ast.unparse(node.annotation) == "Any"
        ):
            found.append(f"holds the opaque {node.value.id} as Any")
    return found


# The one slot a Fields or a Reply keeps its value in, and the other doors to it: reflection by
# the slot's name, pickle and copy's hooks, and the readers of an object's referents and members.
# No product module outside the providers uses any of them.
SLOT = "_wire_value"
STATE_HOOKS = {"__reduce__", "__reduce_ex__", "__getstate__"}
REFLECTORS = {"gc": {"get_referents", "get_referrers", "get_objects"}, "inspect": {"getmembers"}}


def reads_the_slot(tree: ast.AST, docstrings: set[int]) -> list[str]:
    """The identifiers and strings that name the slot an opaque value keeps its content in, the
    hooks that would hand it over, and gc.get_referents or inspect.getmembers, under any import
    form: each reads the wire where the type checker cannot see it."""
    found: list[str] = []
    modules = {name: imported(tree, {name})[0] for name in REFLECTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SLOT in node.value and id(node) not in docstrings:
                found.append(f"names the wire slot in the string {node.value!r}")
            if node.value in STATE_HOOKS and id(node) not in docstrings:
                found.append(f"names {node.value}, which hands over an opaque value's state")
        for name in (
            getattr(node, "id", None),
            getattr(node, "attr", None),
            getattr(node, "arg", None),
            getattr(node, "name", None),
        ):
            if isinstance(name, str) and SLOT in name:
                found.append(f"names {name}, the slot of an opaque wire value")
            # A class may define a hook of its own (a str subclass that pickles itself); only
            # a name or an attribute that is read, to call it on something, is the door.
            read = isinstance(node, ast.Name | ast.Attribute)
            if read and isinstance(name, str) and name in STATE_HOOKS:
                found.append(f"uses {name}, which hands over an opaque value's state")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            for library, readers in REFLECTORS.items():
                if node.value.id in modules[library] and node.attr in readers:
                    found.append(f"reads an object's content with {library}.{node.attr}")
        if isinstance(node, ast.ImportFrom) and (node.module or "") in REFLECTORS:
            readers = REFLECTORS[node.module or ""]
            found += [
                f"imports {node.module}.{a.name}, which reads an object's content"
                for a in node.names
                if a.name in readers or a.name == "*"
            ]
    return found


def names_in_strings(tree: ast.AST, docstrings: set[int], module: str) -> list[str]:
    """The strings of this tree that name a provider, or a provider's module."""
    names = {p.name for p in builtin_providers()} | COMING_PROVIDERS
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        text = node.value
        if not isinstance(text, str):
            continue
        if text in names and module not in NAMES_PROVIDERS:
            found.append(f"names the provider {text!r}")
        if any(part in text for part in MODULE_NAMES):
            found.append(f"names a provider's module in the string {text!r}")
    return found


def problems_in(source: str, module: str = "") -> list[str]:
    """What in this source reaches a provider, or decides by the wire form of an API of one.
    module is the file's name, for the few that may name a provider or hold a path pattern."""
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
            elif node.value in keys:
                found.append(f"names the wire key {node.value!r}")
    return (
        found
        + paths_in_patterns(tree, module)
        + names_in_strings(tree, docstrings, module)
        + parses_json(tree, module)
        + probes_opaque(tree)
        + reads_the_slot(tree, docstrings)
    )


@pytest.mark.parametrize("path", product_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_product_module_reaches_a_provider_or_decides_by_one_of_its_apis(path: Path) -> None:
    """The gate, the changes, the run's state, the config, init and every other module know a
    provider only through the interface and the registry."""
    bad = problems_in(path.read_text(), path.name)
    assert not bad, f"{path.relative_to(SRC)}: {bad}"


def test_the_literals_the_guard_looks_for_come_from_every_registered_provider() -> None:
    assert set(CASES) == {p.name for p in builtin_providers()}
    literals = api_literals()
    assert {"/responses", "responses", "/chat/completions", "chat/completions"} <= literals
    assert {"/messages", "messages", "messages/count_tokens"} <= literals
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
        # Review of the seam, AN-A3-1: a name, a pattern or a module string the lists miss.
        ("def g(call):\n    return call.provider == 'openai'\n", "names the provider 'openai'"),
        (
            "def g(call):\n    return call.provider == 'anthropic'\n",
            "names the provider 'anthropic'",
        ),
        ("def g(p):\n    return p in ('openai', 'x')\n", "names the provider 'openai'"),
        ("def g(path):\n    return 'messages' in path\n", "names the API path 'messages'"),
        (
            "def g(path):\n    return path.rsplit('/', 1)[-1] == 'messages'\n",
            "names the API path 'messages'",
        ),
        (
            "import re\ndef g(p):\n    return re.search(r'/messages$', p)\n",
            "matches a path with re.search, '/messages$'",
        ),
        (
            "import re\nP = re.compile('/v1/other/[a-z]+')\n",
            "matches a path with re.compile, '/v1/other/[a-z]+'",
        ),
        (
            "def g(p):\n    match p:\n        case '/v1/zzz':\n            return 1\n",
            "matches a path in a case, '/v1/zzz'",
        ),
        (
            "def g(p):\n    match p:\n        case 'a' | '/b':\n            return 1\n",
            "matches a path in a case, '/b'",
        ),
        # Review of the opaque type, AN-C0-1 to AN-C0-3: the bypasses that passed before.
        (
            "from scrimmage.providers.impl.wire import fields_dict\n",
            "imports from scrimmage.providers.impl.wire",
        ),
        (
            "from scrimmage.providers.impl import wire\n",
            "imports scrimmage.providers.impl.wire",
        ),
        (
            "from re import search\ndef g(p):\n    return search(r'/messages$', p)\n",
            "matches a path with search, '/messages$'",
        ),
        (
            "from re import compile as c\nP = c('x/messages')\n",
            "matches a path with c, 'x/messages'",
        ),
        (
            "import re as r\ndef g(p):\n    return r.match('/v1/zzz', p)\n",
            "matches a path with r.match, '/v1/zzz'",
        ),
        (
            "import regex\ndef g(p):\n    return regex.search('/v1/zzz', p)\n",
            "matches a path with regex.search, '/v1/zzz'",
        ),
        (
            "import re\ndef g(p):\n    return re.compile(pattern=r'/v1/zzz')\n",
            "matches a path with re.compile, '/v1/zzz'",
        ),
        (
            "import re\nP = '/messages$'\ndef g(p):\n    return re.search(P, p)\n",
            "matches a path with re.search, '/messages$'",
        ),
        (
            "import re\ndef g(p):\n    return re.search(r'.*/messages$', p)\n",
            "matches a path with re.search, '.*/messages$'",
        ),
        (
            "import re\ndef g(p):\n    return re.search('v1/' + 'chat/completions', p)\n",
            "matches a path with re.search, 'v1/chat/completions'",
        ),
        ("from re import *\n", "imports * from a regex library"),
        (
            "from scrimmage.providers.base import Fields\n"
            "def g(fields: Fields):\n    return getattr(fields, 'get')('stream')\n",
            "probes the opaque fields with getattr",
        ),
        (
            "from scrimmage.providers.base import Fields\n"
            "def g(fields: Fields):\n    return isinstance(fields, dict)\n",
            "probes the opaque fields with isinstance",
        ),
        (
            "from scrimmage.providers.base import Fields\n"
            "def g(fields: Fields):\n    return \"'stream': True\" in repr(fields)\n",
            "probes the opaque fields with repr",
        ),
        (
            "from typing import Any, cast\nfrom scrimmage.providers.base import Fields\n"
            "def g(fields: Fields):\n    return cast(Any, fields).get('stream')\n",
            "probes the opaque fields with cast",
        ),
        (
            "from typing import Any\nfrom scrimmage.providers.base import Fields\n"
            "def g(fields: Fields):\n    held: Any = fields\n    return held\n",
            "holds the opaque fields as Any",
        ),
        (
            "from scrimmage.providers.base import Fields\n"
            "def g(provider, body):\n    asked = provider.request_fields(body)\n"
            "    return vars(asked), str(asked)\n",
            "probes the opaque asked with vars",
        ),
        (
            "from scrimmage.providers.base import Reply\n"
            "def g(reply: Reply):\n    return f'{reply}'\n",
            "formats the opaque reply",
        ),
        (
            "def g():\n    return sys.modules['scrimmage.provider_openai']\n",
            "names a provider's module in the string 'scrimmage.provider_openai'",
        ),
        (
            "def g():\n    return importlib.import_module('scrimmage.provider_' + 'openai')\n",
            "names a provider's module in the string 'scrimmage.provider_'",
        ),
        (
            "def g():\n    return importlib.import_module('scrimmage.providers.impl.openai')\n",
            "names a provider's module in the string 'scrimmage.providers.impl.openai'",
        ),
        # Review of the opaque class, AN-C1-1 and AN-C1-2: reflection by the slot's name.
        ("def g(fields):\n    return fields._wire_value\n", "names _wire_value, the slot"),
        (
            "def g(fields):\n    return getattr(fields, '_wire_value')\n",
            "names the wire slot in the string '_wire_value'",
        ),
        (
            "def g(step):\n    return getattr(step.fields, '_wire_value')\n",
            "names the wire slot in the string '_wire_value'",
        ),
        (
            "import operator\ndef g(f):\n    return operator.attrgetter('_wire_value')(f)\n",
            "names the wire slot in the string '_wire_value'",
        ),
        (
            "def g(f):\n    return object.__getattribute__(f, '_wire_value')\n",
            "names the wire slot in the string '_wire_value'",
        ),
        (
            "import inspect\ndef g(f):\n    return dict(inspect.getmembers(f))\n",
            "reads an object's content with inspect.getmembers",
        ),
        (
            "import gc\ndef g(f):\n    return gc.get_referents(f)[0]\n",
            "reads an object's content with gc.get_referents",
        ),
        (
            "import gc as g\nx = g.get_referents\n",
            "reads an object's content with gc.get_referents",
        ),
        ("from gc import get_referents\n", "imports gc.get_referents"),
        ("from inspect import getmembers as m\n", "imports inspect.getmembers"),
        ("def g(f):\n    return f.__reduce_ex__(2)\n", "uses __reduce_ex__"),
        ("def g(f):\n    return f.__reduce__()\n", "uses __reduce__"),
        ("def g(f):\n    return object.__reduce_ex__(f, 2)\n", "uses __reduce_ex__"),
        ("def g(f):\n    return getattr(f, '__getstate__')()\n", "names __getstate__"),
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
        "import re\nP = re.compile(r'\\d+/\\d+')\nQ = re.sub('a/b', '', 'a/b')\n",
        "def g(p):\n    match p:\n        case 'chat' | 'x/y':\n            return 1\n",
        'def g():\n    """Reads scrimmage.provider_openai, the openai one."""\n    return 1\n',
        "def g(x):\n    return x.replace('openai-key', 'k').split('/')\n",
        "import re as r\nP = r.compile(r'\\d+')\nQ = r.sub('[a-z]', '', 'x')\n",
        "from re import search\ndef g(p):\n    return search(r'\\d+', p)\n",
        "from scrimmage.providers.base import Fields\n"
        "def g(fields: Fields, provider):\n    return provider.model_name(fields), str(7)\n",
        "def g(x):\n    return isinstance(x, dict) and getattr(x, 'get')\n",
        "import inspect\ndef g(x):\n    return inspect.signature(x), inspect.isclass(x)\n",
        "import gc\ndef g():\n    gc.collect()\n",
        "class E(str):\n    def __reduce__(self):\n        return E, (str(self),)\n",
    ],
)
def test_the_guard_leaves_alone_what_a_module_may_use(fine: str) -> None:
    assert problems_in(GATE + fine) == []


@pytest.mark.parametrize("module", sorted(NO_JSON_PARSING))
@pytest.mark.parametrize(
    ("reads", "says"),
    [
        ("import json\ndef g(b):\n    return json.loads(b)['content']\n", "json.loads"),
        ("import json\ndef g(f):\n    return json.load(f)\n", "json.load"),
        ("import orjson\ndef g(b):\n    return orjson.loads(b)\n", "orjson.loads"),
        ("import ujson\ndef g(b):\n    return ujson.loads(b)\n", "ujson.loads"),
        ("import simplejson\ndef g(b):\n    return simplejson.loads(b)\n", "simplejson.loads"),
        ("from json import loads\n", "imports json.loads"),
        (
            "import json\ndef g(b):\n    return json.JSONDecoder().decode(b)['content']\n",
            "parses JSON with json.JSONDecoder",
        ),
        (
            "import json\ndef g(b):\n    return json.JSONDecoder().raw_decode(b)\n",
            "parses JSON with json.JSONDecoder",
        ),
        ("def g(d, b):\n    return d.raw_decode(b)\n", "parses JSON with d.raw_decode"),
        ("import json as j\ndef g(b):\n    return j.loads(b)\n", "parses JSON with j.loads"),
        ("from json import JSONDecoder\n", "imports json.JSONDecoder"),
        ("from json.decoder import JSONDecoder\n", "imports json.decoder.JSONDecoder"),
        ("import json.decoder\n", "imports json.decoder, a JSON reader"),
        ("import json\nD = json.decoder.JSONDecoder\n", "parses JSON with json.decoder"),
    ],
)
def test_the_gate_and_the_run_state_never_parse_json_themselves(
    module: str, reads: str, says: str
) -> None:
    bad = problems_in(reads, module)
    assert any(says in problem for problem in bad), bad
    assert problems_in(reads, "cli.py") == [], "only the listed modules are held to it"


def test_show_reads_the_records_it_prints_and_nothing_else_as_json() -> None:
    """Its two record readers are the only places show parses JSON: a reply goes to the
    provider as bytes."""
    source = (SRC / "show.py").read_text()
    assert parses_json(ast.parse(source), "show.py") == []
    assert sorted(JSON_SITES) == [("show.py", "summary_of"), ("show.py", "timeline")]
    tree = ast.parse(source)
    inside = enclosing_functions(tree)
    sites = {
        inside[id(n)]
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "loads" and id(n) in inside
    }
    assert sites == {"summary_of", "timeline"}


def test_writing_json_is_no_parsing() -> None:
    writes = "import json\ndef g(x):\n    return json.dumps(x).encode()\n"
    assert problems_in(writes, "http_gate.py") == []
    assert problems_in("import json\nfrom json import dumps\n", "state.py") == []


def test_only_the_vocabulary_modules_may_name_a_provider() -> None:
    naming = "LIBRARIES = ('openai', 'anthropic')\n"
    assert problems_in(naming, "labels.py") == []
    assert problems_in(naming, "init.py") == []
    assert len(problems_in(naming, "http_gate.py")) == 2


def test_only_the_three_own_patterns_may_start_with_a_slash() -> None:
    own = "import re\nP = re.compile(r'/{2,}')\n"
    assert problems_in(own, "credentials.py") == []
    assert problems_in(own, "http_gate.py") != []
    assert problems_in(own.replace("{2,}", "{3,}"), "credentials.py") != []


def test_a_module_that_prints_what_a_model_said_may_not_name_the_keys_of_its_reply() -> None:
    """A report reads a recorded reply through the provider, as the gate does."""
    shows = "def g(r):\n    return r.get('choices')\n"
    assert problems_in(shows)
    assert problems_in("def g(r):\n    return r['message']['tool_calls']\n")


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


def test_the_coming_sets_name_nothing_that_is_registered() -> None:
    """Registering a provider turns what the guard knew of it ahead of time into its contract
    case, so the coming-sets must then be cleaned: this fails until they are."""
    registered = {p.name for p in builtin_providers()}
    assert not COMING_PROVIDERS & registered, "a coming provider is registered: remove its name"
    paths = {path for case in CASES.values() for _, path, _ in case.ops}
    forms = set(paths).union(*(path_forms(path) for path in paths))
    assert not COMING_PATHS & forms, "a coming path is a registered path: remove it"
    keys = {key for case in CASES.values() for key in case.wire_keys}
    assert not COMING_KEYS & keys, "a coming key is a registered key: remove it"
    assert not COMING_PROVIDERS & set(labels.PROVIDERS)


def test_every_table_module_may_open_the_wire_types_and_no_other_module_may() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    (contract,) = [c for c in contracts if c["name"].startswith("Only the providers package")]
    for table in table_modules():
        line = f"scrimmage.{table} -> scrimmage.providers.impl.wire"
        assert line in contract["ignore_imports"], f"{table} cannot open the wire types"
    assert len([i for i in contract["ignore_imports"] if i.endswith("impl.wire")]) == len(
        table_modules()
    ), "a module that is no table module may open the wire types"
    base = (SRC / "providers" / "base.py").read_text()
    for helper in ("fields_dict", "wrap_fields", "reply_value"):
        assert helper not in base, f"{helper} is public in the interface module"
