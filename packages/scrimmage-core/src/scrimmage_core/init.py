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
import shlex
import sys
import tomllib
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from scrimmage_core import probe as probing
from scrimmage_core import toml_write
from scrimmage_core.config import IMPORT_PATH, Agent, Config, Test
from scrimmage_core.outcomes import INFRA, MISTAKE, MISTAKES, NOTES, OK, WARNINGS
from scrimmage_core.provider_openai import HOSTS
from scrimmage_core.runner import Runner, RunResult

PROBE_QUESTION = "scrimmage init probe"
PROBE_TIMEOUT_S = 120.0
FILE_CAP = 5 * 1024 * 1024  # a larger .py file is generated, and is not read
FILE_LIMIT = 5000  # .py files read; a larger tree is cut here, in a fixed order
SHOWN_ENTRIES = 5  # other entry points the draft names in comments
WIDTH = len("model_hosts")  # the settings' values start in one column
QUESTION_NAMES = ("question", "query", "prompt", "message", "text", "input", "user_input")
LIBRARIES = ("openai", "langchain", "langgraph", "mcp", "httpx")  # what an agent imports
FRAMEWORKS = ("langchain", "langgraph")  # their tools reach a gate with no tools list
WEB_SERVICES = ("fastapi", "flask", "starlette")
TOOL_DECORATORS = ("tool", "is_tool", "function_tool")
CLIENTS = ("OpenAI", "AsyncOpenAI", "AzureOpenAI")
MODEL_CLIENTS = (*CLIENTS, "ChatOpenAI", "AzureChatOpenAI")  # what building an agent's client calls
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
NO_MODEL_REQUEST = "saw no model request through a gated client"
NAMED = "the function named with --probe"
GUESS_COMMENT = "# A guess from reading the code. Check it before you run anything."
FIRST_ONLY = (
    "the probe stops at the first model request, so tools offered later are not seen; "
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
    src_dropped: bool = False  # a module name left out a leading src folder
    definitions: list[Definition] = field(default_factory=list[Definition])
    spec_names: set[str] = field(default_factory=set[str])
    candidates: list[Candidate] = field(default_factory=list[Candidate])
    hosts: set[str] = field(default_factory=set[str])
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


@dataclass
class Probe:
    ran: bool = False
    outcome: str | None = None
    notice: dict[str, object] | None = None
    infra: str | None = None  # the probe itself failed: nothing is written

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
    if probe is not None and not IMPORT_PATH.fullmatch(probe):
        return refused(draft, BAD_PROBE.format(value=probe))
    if not folder.is_dir():
        return refused(draft, f"{folder} is not a folder")
    if os.path.lexists(out):
        return refused(draft, f"{out} exists, and scrimmage init never replaces a file; use --out")
    if not out.parent.is_dir():
        return refused(draft, f"cannot write {out}: its folder does not exist")
    root = folder.resolve()
    unimportable = probe if probe is not None and not has_module(probe, root) else None
    if unimportable is not None:
        probe = None  # nothing is run for it, and it is not drafted as the function
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
    known = result.notice is not None and bool(result.offered)
    tools = probe_tools(result, reading) if known else static_tools(reading)
    found = Findings(
        folder, reading, entries, chosen, result, tools, model_hosts(result, reading), probe
    )
    fill(draft, found)
    if unimportable is not None:
        top = unimportable.split(":")[0].split(".")[0]
        draft.messages.append(
            (
                WARNINGS,
                f"--probe {printable(unimportable)} was not run or drafted: {printable(top)} "
                f"cannot be imported from {printable(root)} or the environment; "
                "install it, or check the name",
            )
        )
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
    for path in python_files(folder, reading):
        if reading.files + len(reading.unread) + len(reading.too_large) >= FILE_LIMIT:
            reading.cut_off = True
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


def python_files(folder: Path, reading: Reading) -> Iterator[Path]:
    """In a fixed order, leaving out library folders and hidden folders, and a symlink that
    points outside the folder, which is not part of the code being read."""
    for parent, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not probing.skipped_folder(name))
        for name in sorted(files):
            path = Path(parent) / name
            if name.endswith(".py") and path.is_file():  # a named pipe would never end
                if path.is_symlink() and not path.resolve().is_relative_to(folder):
                    reading.outside.append(path.relative_to(folder).as_posix())
                    continue
                yield path


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


def client_host(node: ast.Call) -> str | None:
    """The host in OpenAI(base_url="...") when the code writes it as text."""
    if decorator_target(node) not in CLIENTS:
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
    return None if host.lower() in HOSTS else host.lower()


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
    library = next((name for name in LIBRARIES if has_library(imports, name)), None)
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
    return Candidate("function", f"{module}:{node.name}", why, 1 if library else 3, order)


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
    """The hosts to name in the draft. The built-in OpenAI host needs no line."""
    hosts = {host: "static" for host in reading.hosts}
    request = cast("dict[str, str] | None", (result.notice or {}).get("model_request"))
    if request is not None and request["host"] not in HOSTS:
        hosts[request["host"]] = "probe"
    return [{"host": h, "provider": "openai", "found_by": hosts[h]} for h in sorted(hosts)]


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
    return read_probe(result, runner.store.lines(result.run), runner.folder / f"{result.run}.log")


def read_probe(result: RunResult, lines: list[dict[str, object]], log: Path) -> Probe:
    """What ended the probe: each ending has its own words."""
    notice = next((n for n in result.notices if n.get("notice") == "probe"), None)
    if notice is not None:
        return Probe(True, stopped_at_model(notice), notice)
    outcome = result.outcome
    # A function that returned something other than text still ended by itself.
    returned = outcome == "crashed" and any(
        n.get("notice") == "answer_path" for n in result.notices
    )
    if outcome == "answered" or returned:
        return Probe(True, NO_MODEL_REQUEST)
    if outcome in ("crashed", "timed out"):
        return Probe(True, f"{outcome}, see {log}")
    if outcome == "refused call":
        return Probe(True, refused_first(lines))
    return Probe(True, outcome, infra=f"the probe could not run: {outcome}, see {log}")


def stopped_at_model(notice: dict[str, object]) -> str:
    request = cast(dict[str, str], notice["model_request"])
    where = f"{request['method']} {request['path']} on {request['host']}"
    if notice.get("unreadable"):
        return (
            f"stopped at {where}, whose tools could not be read, refused before it was sent; "
            "no tools are known"
        )
    if notice.get("tools"):
        return f"stopped at {where}, refused before it was sent"
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
        mapped, offered = numbers["tools_mapped"], numbers["tools_offered"]
        lines.append(f"tools: {mapped} of {offered} offered to the model mapped")
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
    if reading.cut_off:
        notes.append(
            f"read the first {FILE_LIMIT} .py files and stopped; the tree has more, so run "
            "scrimmage init on the folder that holds the agent"
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
