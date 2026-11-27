"""scrimmage init: draft a scrimmage.toml from reading the code, and from a probe if asked.

The read uses ast and imports nothing, so none of the team's code runs. The entry point it
finds is a guess, and a guess is never run. The probe takes the function to run by name, runs
it up to its first model request, which is refused before it is sent, and reads the tools
that request offers. A script is never probed. The draft never holds a real tool, a test or
a mock, as those are the team's to say, and it never replaces a file.
"""

import ast
import importlib.machinery
import importlib.util
import os
import re
import shlex
import sys
import tomllib
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from scrimmage import probe as probing
from scrimmage import toml_write
from scrimmage.config import IMPORT_PATH, Agent, Config, Test
from scrimmage.outcomes import INFRA, MISTAKE, MISTAKES, NOTES, OK, WARNINGS
from scrimmage.providers.registry import built_in
from scrimmage.runner import Runner, RunResult

NOT_FOUND = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")
PROBE_QUESTION = "scrimmage init probe"
PROBE_TIMEOUT_S = 120.0
FILE_CAP = 5 * 1024 * 1024  # a larger .py file is generated, and is not read
SKIP_COUNT_LIMIT = 100_000  # .py names counted after the cut; more is said as "at least"
FILE_LIMIT = 5000  # .py files read; a larger tree is cut here, in a fixed order
SHOWN_ENTRIES = 5  # other entry points the draft names in comments
WIDTH = len("model_hosts")  # the settings' values start in one column
QUESTION_NAMES = ("question", "query", "prompt", "message", "text", "input", "user_input")
LIBRARIES = ("openai", "anthropic", "langchain", "langgraph", "mcp", "httpx")  # what it imports
MODEL_LIBRARIES = ("openai", "anthropic")  # those whose client speaks to a model provider
FRAMEWORKS = ("langchain", "langgraph")  # their tools reach a gate with no tools list
WEB_SERVICES = ("fastapi", "flask", "starlette")
TOOL_DECORATORS = ("tool", "is_tool", "function_tool")
# Each client class, and the provider whose API it speaks.
CLIENTS = {
    "OpenAI": "openai",
    "AsyncOpenAI": "openai",
    "AzureOpenAI": "openai",
    "Anthropic": "anthropic",
    "AsyncAnthropic": "anthropic",
}
MODEL_CLIENTS = (*CLIENTS, "ChatOpenAI", "AzureChatOpenAI", "ChatAnthropic")  # what building calls
NAME_ARGUMENT_DECORATORS = ("tool",)  # LangChain's @tool("name"); elsewhere text is a description
READ_PREFIXES = ("get_", "list_", "search_", "find_", "lookup_", "read_", "check_", "fetch_")
FOUND_BY = ("probe", "static")  # how a tool or a model host was found
PROBLEMS = ("ambiguous", "nested", "not_found")  # why a tool could not be mapped to one path
RISK_NOTE = (
    "the probe ran your code up to its first model request; a tool function called before "
    "then, or a write through a library Scrimmage does not gate, would have run for real"
)
BAD_PROBE = (
    "--probe takes the function to run, as module:function, such as acme.agent:answer; "
    "got {value!r}. A script is never probed; nothing was run"
)
SERVICE_PROBE = (
    "init --probe does not work with an agent inside a web service in this version; probe "
    "the function its endpoint calls, as module:function; nothing was run"
)
NO_MODEL_REQUEST = "saw no model request through a gated client"
NAMED = "the function named with --probe"
GUESS_COMMENT = "# A guess from reading the code. Check it before you run anything."
FIRST_ONLY = (
    "the probe stops at the first model request that offers tools, so tools offered later "
    "are not seen; "
    "the candidates from reading the code stand"
)
NO_ENTRY = (
    "found no entry point: a function that takes the question, or a script that reads it; "
    "fill in function or command under [agent]"
)


@dataclass(frozen=True)
class Definition:
    """A function or method the read found."""

    path: str  # module:function or module:Class.method
    name: str  # its own name
    nested: bool  # on a class inside a class, which this version cannot gate
    decorated: bool  # carries a tool decorator
    model_name: str | None  # the name its decorator gives the model, if a literal one
    framework: bool  # a tool a framework's adapter sees, so it needs no tools list


@dataclass(frozen=True)
class Candidate:
    """A possible entry point."""

    kind: str  # function or command
    value: str
    why: str
    rank: int
    order: tuple[object, ...]  # ties in a rank: a test file last, then the likelier name


@dataclass
class Reading:
    """What the read found across every file."""

    files: int = 0
    unread: list[str] = field(default_factory=list[str])
    too_large: list[str] = field(default_factory=list[str])
    outside: list[str] = field(default_factory=list[str])  # symlinks that leave the folder
    cut_off: bool = False  # the tree had more .py files than FILE_LIMIT
    skipped: int = 0  # the .py files after the cut, counted and not read
    src_dropped: bool = False  # a module name left out a leading src folder
    top_level: dict[str, str] = field(default_factory=dict[str, str])  # folder: a module under it
    unimportable: list[str] = field(default_factory=list[str])  # files with defs and no name
    definitions: list[Definition] = field(default_factory=list[Definition])
    spec_names: set[str] = field(default_factory=set[str])
    candidates: list[Candidate] = field(default_factory=list[Candidate])
    hosts: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])  # host, provider
    web_service: bool = False


@dataclass(frozen=True)
class Tool:
    path: str
    name: str  # as the model sees it
    found_by: str
    renamed: bool = False  # the model sees another name, so the draft says both


@dataclass(frozen=True)
class Unresolved:
    name: str
    candidates: tuple[str, ...]
    problem: str


@dataclass
class Tools:
    mapped: list[Tool] = field(default_factory=list[Tool])
    adapter: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # name, found_by
    unresolved: list[Unresolved] = field(default_factory=list[Unresolved])
    gates: dict[str, str] = field(default_factory=dict[str, str])  # adapter tool: its adapter


@dataclass
class Probe:
    ran: bool = False
    outcome: str | None = None
    notice: dict[str, object] | None = None
    infra: str | None = None  # the probe itself failed: nothing is written
    missing: str | None = None  # the probed module, or a package above it, was not found

    @property
    def offered(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], (self.notice or {}).get("tools", []))


@dataclass
class Findings:
    """Everything the read and the probe found, that the draft and the output are made from."""

    folder: Path
    reading: Reading
    entries: list[Candidate]
    chosen: Candidate | None
    result: Probe
    tools: Tools
    hosts: list[dict[str, str]]
    function: str | None  # the function --probe named; without it, the entry point is a guess

    @property
    def guess(self) -> bool:
        return self.chosen is not None and self.function is None


@dataclass
class Draft:
    """What scrimmage init will write and say."""

    out: Path
    shown_out: str
    code: int = OK
    text: str | None = None
    fields: dict[str, object] = field(default_factory=lambda: empty_fields())
    messages: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    lines: list[str] = field(default_factory=list[str])
    last: str = ""


def empty_fields() -> dict[str, object]:
    """The document's own fields, as they are when init stops before it has any to give."""
    return {
        "out": None,
        "entry": None,
        "probe": {"ran": False, "function": None, "outcome": None},
        "numbers": None,
        "tools": [],
        "adapter_tools": [],
        "unresolved": [],
        "model_hosts": [],
    }


def prepare(
    folder: Path,
    out: Path,
    *,
    probe: str | None,
    stop_flag: list[str] | None = None,
    announce: Callable[[str], None] = lambda line: None,
) -> Draft:
    """Read the code and, if asked, probe the named function. Nothing is written yet."""
    draft = Draft(out, str(out))
    # All are checked first, so the team's code never runs for a draft that cannot be written.
    if probe is not None and probe.lower().startswith(("http://", "https://")):
        return refused(draft, SERVICE_PROBE)
    if probe is not None and not IMPORT_PATH.fullmatch(probe):
        return refused(draft, BAD_PROBE.format(value=probe))
    if not folder.is_dir():
        return refused(draft, f"{folder} is not a folder")
    if os.path.lexists(out):
        return refused(draft, f"{out} exists, and scrimmage init never replaces a file; use --out")
    if not out.parent.is_dir():
        return refused(draft, f"cannot write {out}: its folder does not exist")
    root = folder.resolve()
    if probe is not None and not has_module(probe, root):
        top = probe.split(":")[0].split(".")[0]
        return refused(
            draft,
            f"--probe {printable(probe)}: {printable(top)} cannot be imported from "
            f"{printable(root)} or the environment; install it, or fix the name. "
            "Nothing was run",
        )
    reading = read_folder(root)
    entries = sorted(entry_points(reading), key=lambda c: (c.rank, c.order))
    if probe is not None:
        named_entry = Candidate("function", probe, NAMED, 0, ())
        entries = [named_entry, *(c for c in entries if c.value != probe)]
    chosen = entries[0] if entries else None
    result = Probe()
    if probe is not None:
        announce(f"probe: running {probe} up to its first model request")
        result = run_probe(probe, root, stop_flag)
    if probe is not None and result.missing is not None:
        return refused(
            draft,
            f"--probe {printable(probe)}: {printable(result.missing)} cannot be imported from "
            f"{printable(root)} or the environment; install it, or fix the name",
        )
    known = result.notice is not None and bool(result.offered)
    tools = probe_tools(result, reading) if known else static_tools(reading)
    found = Findings(
        folder, reading, entries, chosen, result, tools, model_hosts(result, reading), probe
    )
    fill(draft, found)
    return draft


def has_module(function: str, folder: Path) -> bool:
    """Whether the function's top-level module can be found, without importing it."""
    top = function.split(":")[0].split(".")[0]
    try:
        found = importlib.util.find_spec(top)
        found = found or importlib.machinery.PathFinder.find_spec(top, [str(folder)])
    except (ImportError, ValueError):
        return False
    return found is not None


def refused(draft: Draft, message: str, last: str = "nothing was written") -> Draft:
    draft.code = MISTAKE
    draft.messages.append((MISTAKES, message))
    draft.last = last
    return draft


def printable(text: object) -> str:
    """Text a terminal shows as written: a newline, an escape or a name that is not UTF-8 is
    escaped, so a name from the code cannot forge a line or fail the print."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(text))


# The read


def read_folder(folder: Path) -> Reading:
    """Every .py file under the folder, by ast. None of them is imported or run."""
    reading = Reading()
    paths = python_files(folder)
    for path in paths:
        if not usable(path, folder, reading):
            continue
        if reading.files + len(reading.unread) + len(reading.too_large) >= FILE_LIMIT:
            reading.cut_off = True
            # Counted by name alone, with no stat and no parse, up to a bound, so the cap
            # still bounds the time. A name that is not a file or a symlink out is counted too.
            reading.skipped = 1 + sum(1 for _ in islice(paths, SKIP_COUNT_LIMIT))
            break
        relative = path.relative_to(folder).as_posix()
        shown = relative.encode("utf-8", "backslashreplace").decode()
        if over_the_cap(path):
            reading.too_large.append(shown)
            continue
        tree = parse(path)
        if tree is None:
            reading.unread.append(shown)
            continue
        try:
            read_module(tree, module_name(folder, path, reading), relative, reading)
        except RecursionError:  # a tree too deep to walk is a file the read cannot use
            reading.unread.append(shown)
            continue
        reading.files += 1
    return reading


def python_files(folder: Path) -> Iterator[Path]:
    """Every name ending in .py, in a fixed order, leaving out library folders and hidden
    folders. Nothing is asked of the file system but the listing."""
    for parent, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not probing.skipped_folder(name))
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(parent) / name


def usable(path: Path, folder: Path, reading: Reading) -> bool:
    """Whether to read it: a file, not a named pipe that would never end, and not a symlink
    that points outside the folder, which is not part of the code being read."""
    if not path.is_file():
        return False
    if path.is_symlink() and not path.resolve().is_relative_to(folder):
        reading.outside.append(path.relative_to(folder).as_posix())
        return False
    return True


def over_the_cap(path: Path) -> bool:
    try:
        return path.stat().st_size > FILE_CAP
    except OSError:
        return False  # parse says it could not read it


def parse(path: Path) -> ast.Module | None:
    """The file's syntax tree, or None if it cannot be read as Python."""
    try:
        str(path).encode()  # a name that is not text cannot go in a file or a document
        with warnings.catch_warnings():  # the team's bad escapes must not reach standard error
            warnings.simplefilter("ignore")
            return ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def module_name(folder: Path, path: Path, reading: Reading) -> str:
    """Its import name once its package is installed. A src layout's folder is not a package,
    and a folder that is itself inside a package keeps its packages' names."""
    parts = list(path.relative_to(folder).with_suffix("").parts)
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
        reading.src_dropped = True
    if parts[-1] == "__init__":
        parts.pop()
    packages: list[str] = []
    above = folder
    while (above / "__init__.py").is_file():
        if not above.name.isidentifier():
            # No code can import this folder by name, so its children are the top level, as
            # they are when the team runs from inside it.
            name = ".".join([*packages, *parts])
            shown = reading.top_level.get(above.name)
            if importable(name) and (shown is None or name.count(".") > shown.count(".")):
                reading.top_level[above.name] = name  # the deepest is the best example
            break
        packages.insert(0, above.name)
        above = above.parent
    return ".".join([*packages, *parts])


def importable(module: str) -> bool:
    return bool(module) and all(part.isidentifier() for part in module.split("."))


def read_module(tree: ast.Module, module: str, relative: str, reading: Reading) -> None:
    imports = imported(tree)
    reading.web_service = reading.web_service or any(root in imports for root in WEB_SERVICES)
    framework = any(has_library(imports, name) for name in FRAMEWORKS)
    builds_client = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and (name := spec_name(node)) is not None:
            reading.spec_names.add(name)
        elif isinstance(node, ast.Call):
            builds_client = builds_client or decorator_target(node) in MODEL_CLIENTS
            if (host := client_host(node)) is not None:
                reading.hosts.add(host)
    if is_script(tree, imports):
        reading.candidates.append(script_candidate(relative))
    if not importable(module):
        if any(
            isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) for s in tree.body
        ):
            reading.unimportable.append(relative)
        return
    for statement in top_level(tree.body):
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            reading.definitions.append(
                definition(f"{module}:{statement.name}", statement, framework=framework)
            )
            found = function_candidate(statement, module, imports, relative, builds_client)
            if found is not None:
                reading.candidates.append(found)
        elif isinstance(statement, ast.ClassDef):
            read_class(
                statement, f"{module}:{statement.name}", reading, framework=framework, nested=False
            )


def top_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """A module's statements, with those inside a top-level if or try."""
    # A stack, not recursion: an elif chain is an If nested in the last one's orelse.
    stack = list(reversed(body))
    while stack:
        statement = stack.pop()
        yield statement
        inner: list[ast.stmt] = []
        if isinstance(statement, ast.If):
            inner = [*statement.body, *statement.orelse]
        elif isinstance(statement, ast.Try):
            inner = [*statement.body, *statement.orelse]
            for handler in statement.handlers:
                inner += handler.body
        stack.extend(reversed(inner))


def read_class(
    node: ast.ClassDef, owner: str, reading: Reading, *, framework: bool, nested: bool
) -> None:
    for item in node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            path = f"{owner}.{item.name}"
            reading.definitions.append(definition(path, item, framework=framework, nested=nested))
        elif isinstance(item, ast.ClassDef):
            read_class(item, f"{owner}.{item.name}", reading, framework=framework, nested=True)


def definition(
    path: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    framework: bool,
    nested: bool = False,
) -> Definition:
    tool = next((d for d in node.decorator_list if tool_decorator(d)), None)
    return Definition(
        path,
        node.name,
        nested,
        tool is not None,
        None if tool is None else decorator_name(tool),
        framework and tool is not None,
    )


def decorator_target(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


def tool_decorator(node: ast.expr) -> bool:
    name = decorator_target(node)
    return name in TOOL_DECORATORS or name.endswith("_tool")


def decorator_name(node: ast.expr) -> str | None:
    """The name a tool decorator gives the model, when the code writes it as text."""
    if not isinstance(node, ast.Call):
        return None
    positional = decorator_target(node) in NAME_ARGUMENT_DECORATORS and node.args
    given = [node.args[0]] if positional else []
    given += [k.value for k in node.keywords if k.arg in ("name", "name_override")]
    return next(
        (v.value for v in given if isinstance(v, ast.Constant) and isinstance(v.value, str)), None
    )


def imported(tree: ast.Module) -> set[str]:
    """The top-level names the module imports, anywhere in it."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def has_library(imports: set[str], library: str) -> bool:
    """The library or one of its parts, as langchain_openai is langchain's."""
    return any(root == library or root.startswith(f"{library}_") for root in imports)


def spec_name(node: ast.Dict) -> str | None:
    """The name in a tool spec written as a dict literal, in either of OpenAI's two forms."""
    fields: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            fields[key.value] = value
    is_function = text_of(fields.get("type")) == "function"
    if "parameters" not in fields and not is_function:
        return None
    inner = fields.get("function")
    if is_function and isinstance(inner, ast.Dict):
        return None  # the inner dict has the name, and the walk reaches it
    return text_of(fields.get("name"))


def text_of(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def client_host(node: ast.Call) -> tuple[str, str] | None:
    """The host and its provider in OpenAI(base_url="...") when the code writes the host as
    text. A provider's own host is none: it needs no line."""
    provider = CLIENTS.get(decorator_target(node))
    if provider is None:
        return None
    url = next((text_of(k.value) for k in node.keywords if k.arg == "base_url"), None)
    if url is None:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or ":" in host:
        return None
    host = host.lower()
    return None if host in built_in().get(provider).hosts else (host, provider)


def is_script(tree: ast.Module, imports: set[str]) -> bool:
    """It runs by itself, and reads its command line."""
    main = any(isinstance(s, ast.If) and is_main_test(s.test) for s in tree.body)
    return main and ("argparse" in imports or reads_argv(tree))


def is_main_test(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and text_of(test.comparators[0]) == "__main__"
    )


def reads_argv(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "argv"
        and isinstance(n.value, ast.Name)
        and n.value.id == "sys"
        for n in ast.walk(tree)
    )


def script_candidate(relative: str) -> Candidate:
    return Candidate(
        "command",
        f"python {shlex.quote(relative)} {{question}}",
        "a script that reads its command line",
        2,
        (test_file(relative), relative),
    )


def test_file(relative: str) -> bool:
    return any(part.startswith(("test_", "tests", "conftest")) for part in relative.split("/"))


def function_candidate(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    imports: set[str],
    relative: str,
    builds_client: bool,
) -> Candidate | None:
    """A public top-level function with one required argument, named for a question."""
    if node.name.startswith("_") or any(tool_decorator(d) for d in node.decorator_list):
        return None
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    required = positional[: len(positional) - len(arguments.defaults)]
    if len(required) != 1 or required[0].arg not in QUESTION_NAMES:
        return None
    if any(default is None for default in arguments.kw_defaults):
        return None  # a required keyword: the starter passes the question alone
    found = [name for name in LIBRARIES if has_library(imports, name)]
    models = [name for name in found if name in MODEL_LIBRARIES]
    library = " and ".join(models if len(models) > 1 else found[:1])  # both clients are named
    named = f"one argument, named {required[0].arg}"
    why = f"{named}, in a module that imports {library}" if library else named
    client = builds_client
    if client:
        why += " and builds a model client"
    order = (
        test_file(relative),
        not client,
        QUESTION_NAMES.index(required[0].arg),
        module,
        node.name,
    )
    return Candidate("function", f"{module}:{node.name}", why, 1 if found else 3, order)


def entry_points(reading: Reading) -> list[Candidate]:
    """Every candidate that is not itself a tool."""
    tools = {d.path for d in reading.definitions if d.decorated or d.name in reading.spec_names}
    return [c for c in reading.candidates if c.value not in tools]


# Names to paths


def static_tools(reading: Reading) -> Tools:
    """The tools the read found: a tool decorator, or a name in a tool spec."""
    tools = Tools()
    names: dict[str, list[Definition]] = {}
    for item in reading.definitions:
        if item.decorated:
            names.setdefault(item.model_name or item.name, []).append(item)
    for name in reading.spec_names:
        if name not in names:
            names[name] = [d for d in reading.definitions if d.name == name]
    for name in sorted(names):
        matches = names[name]
        if matches and all(m.framework for m in matches):
            tools.adapter.append((name, "static"))
        else:
            place(tools, name, matches, "static")
    return tools


def place(tools: Tools, name: str, matches: list[Definition], found_by: str) -> None:
    """One match is a path. None, several, or one that cannot be gated is listed instead."""
    paths = tuple(sorted({m.path for m in matches}))  # defined in both arms of an if: one path
    if not matches:
        tools.unresolved.append(Unresolved(name, (), "not_found"))
    elif len(paths) > 1:
        tools.unresolved.append(Unresolved(name, paths, "ambiguous"))
    elif matches[0].nested:
        tools.unresolved.append(Unresolved(name, paths, "nested"))
    else:
        tools.mapped.append(Tool(matches[0].path, name, found_by, matches[0].name != name))


def probe_tools(result: Probe, reading: Reading) -> Tools:
    """The names the model was offered win. A name the process could not place in a loaded
    module, such as one imported only when it is first called, is looked up in the read."""
    tools = Tools()
    for offered in result.offered:
        name = str(offered["name"])
        paths = cast(list[str], offered["paths"])
        if offered["adapter"]:
            tools.adapter.append((name, "probe"))
            tools.gates[name] = str(offered.get("gate", "framework adapter"))
        elif len(paths) == 1:
            tools.mapped.append(Tool(paths[0], name, "probe"))
        elif paths:
            tools.unresolved.append(Unresolved(name, tuple(paths), "ambiguous"))
        else:
            place(tools, name, named(reading, name), "probe")
    return tools


def named(reading: Reading, name: str) -> list[Definition]:
    """The definitions a name means; a decorated one says so, and so wins."""
    matches = [d for d in reading.definitions if d.name == name or d.model_name == name]
    return [d for d in matches if d.decorated] or matches


def model_hosts(result: Probe, reading: Reading) -> list[dict[str, str]]:
    """The hosts to name in the draft. A provider's own host needs no line. A host the code
    names for a client class has that client's provider; one the probe stopped at has the
    provider whose path the request was."""
    hosts = {pair: "static" for pair in reading.hosts}
    request = cast("dict[str, str] | None", (result.notice or {}).get("model_request"))
    if request is not None and request["host"] not in built_in().get(request["provider"]).hosts:
        hosts[(request["host"], request["provider"])] = "probe"
    return [
        {"host": h, "provider": provider, "found_by": found_by}
        for (h, provider), found_by in sorted(hosts.items())
    ]


# The probe


def run_probe(function: str, folder: Path, stop_flag: list[str] | None) -> Probe:
    """One run of the function the team named, with no tools, no mocks and no budget. Its
    first model request is refused before it is sent, and any refusal ends the program."""
    agent = Agent(function, 1, 1, PROBE_TIMEOUT_S, (), (), max_cost_usd=None)
    config = Config(agent, (Test("init-probe", PROBE_QUESTION, (), (), ()),), folder)
    runner = Runner(config, stop_flag=stop_flag, probe=True)
    try:
        results = runner.run_all()
    finally:
        runner.server.server.server_close()
    if not results:
        return Probe(ran=True, infra="the probe did not run")
    result = results[0]
    log = runner.folder / f"{result.run}.log"
    return read_probe(result, runner.store.lines(result.run), log, function)


def missing_module(log: Path, function: str) -> str | None:
    """The module the crash log's last traceback ends on, if the error says it was not found
    and it is the probed module or a package above it. An earlier traceback, such as a worker
    that logged one and went on, does not count, and neither does a module the function
    itself imports and cannot find."""
    module = function.split(":")[0]
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _, found, last = text.rpartition("Traceback (most recent call last):")
    if not found:
        return None
    # The frames are indented; the first line that is not is the exception.
    ending = next((line for line in last.splitlines()[1:] if line[:1] not in (" ", "\t")), "")
    named = NOT_FOUND.match(ending)
    if named is None:
        return None
    name = named.group(1)
    return name if module == name or module.startswith(name + ".") else None


def read_probe(
    result: RunResult, lines: list[dict[str, object]], log: Path, function: str
) -> Probe:
    """What ended the probe: each ending has its own words."""
    passes = [line for line in lines if "probe_pass" in line]
    notice = next((n for n in result.notices if n.get("notice") == "probe"), None)
    if notice is not None:
        return Probe(True, stopped_at_model(notice, len(passes)), notice)
    outcome = result.outcome
    # A function that returned something other than text still ended by itself.
    returned = outcome == "crashed" and any(
        n.get("notice") == "answer_path" for n in result.notices
    )
    if outcome == "answered" or returned:
        ended = Probe(True, f"ended by itself{after(len(passes))}" if passes else NO_MODEL_REQUEST)
    elif outcome in ("crashed", "timed out"):
        ended = Probe(
            True,
            f"{outcome}, see {log}{after(len(passes))}",
            missing=missing_module(log, function) if outcome == "crashed" else None,
        )
    elif outcome == "refused call":
        return Probe(True, refused_first(lines))
    else:
        return Probe(True, outcome, infra=f"the probe could not run: {outcome}, see {log}")
    if passes:
        # No request offered tools, so no stop: the first one still names the host and provider.
        names = ("host", "method", "path", "provider")
        request = {name: str(passes[0].get(name, "")) for name in names}
        ended.notice = {"tools": [], "model_request": request}
    return ended


def requests_with_no_tools(count: int) -> str:
    return f"{count} request{'s' if count != 1 else ''} with no tools"


def after(passed: int) -> str:
    """What to add when the probe let tool-less requests through before the run ended: the
    canned reply may be why it ended, as a guardrail that parses a reply would fail on it."""
    if not passed:
        return ""
    return (
        f"; passed {requests_with_no_tools(passed)} first, such as guardrails, with a canned "
        "reply that may have sent the agent down another path"
    )


def stopped_at_model(notice: dict[str, object], passed: int = 0) -> str:
    request = cast(dict[str, str], notice["model_request"])
    where = f"{request['method']} {request['path']} on {request['host']}"
    skipped = f"; passed {requests_with_no_tools(passed)}, such as guardrails" if passed else ""
    if notice.get("unreadable"):
        return (
            f"stopped at {where}, whose tools could not be read, refused before it was sent"
            f"{skipped}; no tools are known"
        )
    if notice.get("tools"):
        return f"stopped at {where}, refused before it was sent{skipped}"
    if passed:
        return (
            f"stopped at {where}, which offers no tools either, refused before it was sent"
            f"{skipped}; no tools are known, and the agent may offer tools later"
        )
    return (
        f"stopped at {where}, which offers no tools, refused before it was sent; no tools are known"
    )


def refused_first(lines: list[dict[str, object]]) -> str:
    refused = next(line for line in lines if line.get("action") == "refused")
    parts = [refused["call"], refused.get("method"), refused.get("path")]
    named_call = " ".join(str(part) for part in parts if part)
    return f"stopped at {named_call}, refused before it ran; no tools are known"


# The draft


def fill(draft: Draft, found: Findings) -> None:
    """Everything init will say, and the text it will write."""
    fill_fields(draft, found)
    say_found(draft, found)
    add_notes(draft, found)
    result = found.result
    if result.infra is not None:
        draft.code = INFRA
        draft.messages.append(("infra", result.infra))
        records = found.folder / "scrimmage-runs"
        draft.last = f"no draft was written; the probe left its records under {records}"
        return
    draft.text = render(found)
    if found.chosen is None:
        draft.code = MISTAKE
        draft.messages.append((MISTAKES, NO_ENTRY))
        draft.last = (
            f"fill in function or command under [agent] in {draft.shown_out}, "
            "then run scrimmage check"
        )
    else:
        draft.last = (
            "next: write one [[test]] with a question and its expect, list the tools that are "
            "safe to run for real under real, then run scrimmage check"
        )
    root = found.folder.resolve()
    if draft.out.resolve().parent != root:
        draft.messages.append(
            (WARNINGS, f"the draft's module paths need it beside the code; move it to {root}")
        )


def numbers_of(found: Findings) -> dict[str, int] | None:
    """Tools mapped over tools offered, when a model request offered any."""
    notice = found.result.notice
    if notice is None or not found.result.offered:
        return None
    mapped = sum(1 for tool in found.tools.mapped if tool.found_by == "probe")
    return {"tools_offered": len(found.result.offered), "tools_mapped": mapped}


def fill_fields(draft: Draft, found: Findings) -> None:
    tools, chosen = found.tools, found.chosen
    entry: dict[str, object] | None = None
    if chosen is not None:
        entry = {
            "kind": chosen.kind,
            "value": chosen.value,
            "guess": found.guess,
            "candidates": [{"value": c.value, "why": c.why} for c in found.entries],
        }
    draft.fields.update(
        entry=entry,
        probe={
            "ran": found.result.ran,
            "function": found.function,
            "outcome": found.result.outcome,
        },
        numbers=numbers_of(found),
        tools=[{"path": t.path, "name": t.name, "found_by": t.found_by} for t in tools.mapped],
        adapter_tools=[{"name": n, "found_by": by} for n, by in tools.adapter],
        unresolved=[
            {"name": u.name, "candidates": list(u.candidates), "problem": u.problem}
            for u in tools.unresolved
        ],
        model_hosts=found.hosts,
    )


def say_found(draft: Draft, found: Findings) -> None:
    tools, chosen, lines = found.tools, found.chosen, draft.lines
    count = found.reading.files
    lines.append(
        f"scrimmage init: read {count} Python files under {printable(found.folder)} "
        "without importing them"
    )
    label = "entry point (a guess)" if found.guess else "entry point"
    lines.append(
        f"{label}: {printable(chosen.value)} ({chosen.why})"
        if chosen
        else "entry point: none found"
    )
    lines.append(probe_line(found))
    numbers = numbers_of(found)
    if numbers is not None:
        lines.append(tools_line(numbers["tools_mapped"], numbers["tools_offered"], tools))
    else:
        lines.append(f"tools: {len(tools.mapped)} found by reading the code")
    lines += [f"  {printable(t.path)}" for t in tools.mapped]
    for item in tools.unresolved:
        where = ", ".join(item.candidates) or "no candidates"
        lines.append(f"  {printable(item.name)}: not mapped ({item.problem}): {printable(where)}")
    if tools.adapter:
        lines.append(f"adapter tools: {printable(', '.join(n for n, _ in tools.adapter))}")
    if found.hosts:
        hosts = ", ".join(f"{h['host']} ({h['provider']})" for h in found.hosts)
        lines.append(f"model hosts: {printable(hosts)}")


def tools_line(mapped: int, offered: int, tools: Tools) -> str:
    """Tools mapped over tools offered. A framework adapter's tools reach a gate by themselves,
    so they count as covered and are never said to be unmapped. Each says which adapter."""
    gated = len(tools.adapter)
    if not gated:
        return f"tools: {mapped} of {offered} offered to the model mapped"
    by_gate = Counter(tools.gates.get(name, "framework adapter") for name, _ in tools.adapter)
    if gated == offered:
        each = ", ".join(f"{n} by the {gate}" for gate, n in by_gate.items())
        return f"tools: {offered} offered to the model, all gated ({each})"
    parts = [f"{offered} offered"]
    if mapped:
        parts.append(f"{mapped} mapped")
    parts += [f"{n} gated by the {gate}" for gate, n in by_gate.items()]
    if left := offered - mapped - gated:
        names = ", ".join(printable(u.name) for u in tools.unresolved)
        parts.append(f"{left} not mapped" + (f": {names}" if names else ""))
    return "tools: " + ", ".join(parts)


def probe_line(found: Findings) -> str:
    """Says how to name a function. It never prints a command for the guess: one that can be
    run as given would run the guess, which may be the wrong function."""
    result, chosen = found.result, found.chosen
    if result.ran and chosen is not None:
        return f"probe: ran {chosen.value} up to its first model request; {result.outcome}"
    if chosen is not None and chosen.kind == "command":
        return "probe: not run; a script is never probed"
    return (
        "probe: not run; to find the tools the model is offered, choose the function to run "
        "and name it: scrimmage init --probe module:function --out another.toml"
    )


def add_notes(draft: Draft, found: Findings) -> None:
    result, reading, tools = found.result, found.reading, found.tools
    notes: list[str] = []
    if result.ran:
        notes.append(RISK_NOTE)
    if result.notice is not None and not result.offered:
        notes.append(FIRST_ONLY)
    elif result.ran and result.notice is None:
        notes.append("no tools are known from the probe; the candidates from the code stand")
    notes += install_notes(found)
    if tools.adapter:
        notes.append("a framework's tools reach a gate by themselves, so they need no tools list")
    if reading.web_service:
        notes.append(
            "this looks like a web service; a service entry point comes in a later version"
        )
    if reading.unread:
        shown = ", ".join(reading.unread[:5])
        notes.append(f"could not read {len(reading.unread)} files as Python: {shown}")
    if reading.too_large:
        shown = ", ".join(reading.too_large[:5])
        notes.append(f"skipped {len(reading.too_large)} files over 5 MB as too large: {shown}")
    if reading.outside:
        shown = ", ".join(reading.outside[:5])
        notes.append(
            f"skipped {len(reading.outside)} .py symlinks that point outside the folder: {shown}"
        )
    for folder, example in reading.top_level.items():
        notes.append(
            f"{printable(folder)} is not an importable package name, so its modules are read as "
            f"top-level ({printable(example)}); run scrimmage from inside {printable(folder)}"
        )
    if reading.unimportable:
        shown = ", ".join(printable(n) for n in reading.unimportable[:5])
        notes.append(
            f"skipped definitions in {len(reading.unimportable)} files whose names cannot be "
            f"imported: {shown}"
        )
    if reading.cut_off:
        more = "at least " if reading.skipped > SKIP_COUNT_LIMIT else ""
        notes.append(
            f"read the first {FILE_LIMIT} .py files and stopped; skipped {more}{reading.skipped} "
            ".py files, so run scrimmage init on the folder that holds the agent"
        )
    left_out = sorted(left_out_tools(reading, tools)) if result.offered else []
    if left_out:
        notes.append(
            "not offered in the first model request, so left out of the draft: "
            + ", ".join(left_out)
        )
    draft.messages += [(NOTES, note) for note in notes]


def install_notes(found: Findings) -> list[str]:
    """What the module names assume. A run imports from the read folder or the environment."""
    notes: list[str] = []
    if found.reading.src_dropped:
        notes.append("module names leave out the src folder, as an installed package's do")
    paths = [t.path for t in found.tools.mapped]
    if found.chosen is not None and found.chosen.kind == "function":
        paths.append(found.chosen.value)
    root = found.folder.resolve()
    missing = sorted(
        {
            top
            for top in (path.split(".")[0].split(":")[0] for path in paths)
            if not (root / top).is_dir() and not (root / f"{top}.py").is_file()
        }
    )
    if missing:
        names = ", ".join(missing)
        notes.append(
            f"{names} cannot be imported from {printable(found.folder)}; these module names "
            "assume the package is installed, so install it, such as with pip install -e ."
        )
    return notes


def left_out_tools(reading: Reading, tools: Tools) -> set[str]:
    """Tools the read found that the probe's request did not offer."""
    seen = (
        {t.name for t in tools.mapped}
        | {n for n, _ in tools.adapter}
        | {u.name for u in tools.unresolved}
    )
    found = static_tools(reading)
    return {t.name for t in found.mapped if t.name not in seen}


def render(found: Findings) -> str:
    chosen, tools, hosts = found.chosen, found.tools, found.hosts
    today = datetime.now(UTC).date().isoformat()
    lines = [f"# Drafted by scrimmage init on {today}. Check every line.", "[agent]"]
    if chosen is None:
        lines += [
            "# scrimmage init found no entry point: a function that takes the question, "
            "or a script that reads it.",
            "# Give one of them:",
            '# function = "package.module:function"',
            '# command  = "python bot.py {question}"',
        ]
    else:
        if found.guess:
            lines.append(GUESS_COMMENT)
        lines += toml_write.assignment(chosen.kind, chosen.value, WIDTH)
        others = found.entries[1:]
        lines += [
            toml_write.comment(f"Other entry points it saw: {c.value} ({c.why})")
            for c in others[:SHOWN_ENTRIES]
        ]
        if len(others) > SHOWN_ENTRIES:
            lines.append(
                f"# And {len(others) - SHOWN_ENTRIES} more; scrimmage init --json lists them."
            )
    if tools.mapped:
        entries_of_tools = [
            {"path": t.path, "name": t.name} if t.renamed else t.path for t in tools.mapped
        ]
        lines += toml_write.assignment("tools", cast(list[object], entries_of_tools), WIDTH)
    for item in tools.unresolved:
        where = ", ".join(item.candidates) or "no match in the code"
        lines.append(toml_write.comment(f"Not listed, {item.problem}: {item.name}: {where}"))
        if item.problem == "nested":
            lines.append(
                "# This version cannot gate a method on a nested class; "
                "move the class to the top of its module."
            )
        elif item.problem == "ambiguous":
            lines.append(
                toml_write.comment(
                    f'Pick one, and list it as {{path = "...", name = "{item.name}"}}.'
                )
            )
    if hosts:
        value: list[object] = [{"host": h["host"], "provider": h["provider"]} for h in hosts]
        lines += toml_write.assignment("model_hosts", value, WIDTH)
    lines += real_lines(tools)
    lines += skeleton()
    text = "\n".join(lines) + "\n"
    read_back(text, tools)
    return text


def real_lines(tools: Tools) -> list[str]:
    """The empty list, and which names look like reads. A comment only: nothing runs from it."""
    lines = ["# List the tools that are safe to run for real. Nothing runs for real until you do."]
    names = [*(t.name for t in tools.mapped), *(n for n, _ in tools.adapter)]
    reads = [name for name in names if name.startswith(READ_PREFIXES)]
    if reads:
        lines.append(
            toml_write.comment(
                f"These only look like reads by their names (a heuristic): {', '.join(reads)}"
            )
        )
    lines.append(f"{'real'.ljust(WIDTH)} = []")
    return lines


def skeleton() -> list[str]:
    return [
        "",
        "# [[test]]",
        '# name     = "order-status"',
        '# question = "Where is order 1234?"',
        '# expect   = ["shipped"]',
    ]


def read_back(text: str, tools: Tools) -> None:
    """The draft must read as TOML, with nothing under real and no test, or it is a bug here."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise toml_write.WriterBug(
            f"scrimmage init wrote a draft tomllib cannot read: {exc}"
        ) from exc
    agent = cast(dict[str, object], data.get("agent", {}))
    if (
        agent.get("real") != []
        or "test" in data
        or len(cast(list[object], agent.get("tools", []))) != len(tools.mapped)
    ):
        raise toml_write.WriterBug("scrimmage init wrote a draft that does not say what was meant")


def write(draft: Draft) -> None:
    """Write the draft, never over a file, even one that appeared since the check."""
    if draft.text is None:
        return
    try:
        with open(draft.out, "x", encoding="utf-8") as output:
            output.write(draft.text)
    except FileExistsError:
        message = f"{draft.shown_out} exists, and scrimmage init never replaces a file; use --out"
        refused(draft, message, "no draft was written")
        return
    except OSError as exc:
        refused(draft, f"could not write {draft.shown_out}: {exc.strerror}", "no draft was written")
        return
    draft.fields["out"] = draft.shown_out


def print_text(draft: Draft) -> None:
    """The text form. Mistakes go to standard error, as scrimmage test sends them."""
    for line in draft.lines:
        print(line)
    draft.messages = [(kind, printable(message)) for kind, message in draft.messages]
    prefixes = {NOTES: "note", WARNINGS: "warning", "infra": "infrastructure error"}
    for kind, message in draft.messages:
        if kind == MISTAKES:
            print(f"scrimmage: {message}", file=sys.stderr)
        else:
            print(f"{prefixes[kind]}: {message}")
    if draft.fields["out"] is not None:
        print(f"wrote {printable(draft.shown_out)}")
    print(printable(draft.last))
