"""What a real call gave, saved only when a mock built from it gives the same back.

A recording run keeps, on the line of each real tool and MCP call, the value as plain
fields and the call's arguments, both cleaned of secrets. The test is the one the team
runs later: the gate builds the mock from the fields exactly as scrimmage test would, and
compares it with the real value by type-strict recursive equality. A value that does not
come back equal is listed, never written, so a mock can never give the agent a value of
another type. The original value goes back to the agent untouched.
"""

import dataclasses
import itertools
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from scrimmage_core import mocks
from scrimmage_core.config import IMPORT_PATH
from scrimmage_core.redact import REMOVED, redacted

INT64 = (-(2**63), 2**63 - 1)  # TOML integers are 64 bits
SHOWN = 60  # how much of a value a "differs" message quotes
# A string value under a key with one of these whole word parts is a secret by its name.
SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "auth",
        "authorization",
        "cookie",
        "session",
        "credential",
        "credentials",
    }
)
SECRET_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key")})
# Word parts: a run of capitals before a capitalised word (API in APIKey), a word, or a run
# of capitals. Anything that is not a letter, such as _, -, ., a space or a digit, splits.
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
KEY_NAME, PATTERN = "key_name", "pattern"
UNSAVABLE, SECRET_CHANGED_SHAPE, STATUS = "unsavable", "secret_changed_shape", "status"

Removed = dict[str, object]


class Unsavable(Exception):
    """A value or argument a mock cannot hold, with the place that is the problem."""


def kind_of(value: object) -> str:
    """A value's type as a person reads it: str, int, a class's own name."""
    return type(value).__name__


def shown(value: object) -> str:
    text = repr(value)
    return f"{kind_of(value)} {text if len(text) <= SHOWN else text[:SHOWN] + '...'}"


def plain(value: object, place: str) -> object:
    """A copy made only of what TOML and JSON hold, read by exact type.

    A subclass, such as a StrEnum, an IntEnum or a defaultdict, is not its base: the mock
    would give back the base, so it raises Unsavable. A tuple becomes a list; whether the
    tool gives back a tuple is for the round trip to decide.
    """
    kind = type(value)
    if kind is int:
        if not INT64[0] <= cast(int, value) <= INT64[1]:
            raise Unsavable(f"{place}: an integer beyond 64 bits, which TOML cannot hold")
        return value
    if value is None or kind in (str, bool, float):
        return value
    if kind is list or kind is tuple:
        items = cast(list[object], value)
        return [plain(item, f"{place}[{i}]") for i, item in enumerate(items)]
    if kind is dict:
        out: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise Unsavable(f"{place}: a key that is not text, {shown(key)}")
            out[key] = plain(item, f"{place}.{key}")
        return out
    raise Unsavable(f"{place}: a {kind_of(value)}, which a mock cannot hold")


def is_model(value: object) -> bool:
    """A pydantic model, read by what it has: the SDK never imports pydantic."""
    return callable(getattr(value, "model_dump", None)) and isinstance(
        getattr(type(value), "model_fields", None), dict
    )


def is_named_tuple(value: object) -> bool:
    kind: type = type(value)
    return issubclass(kind, tuple) and hasattr(cast(object, kind), "_fields")


def is_dataclass_object(value: object) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def fields_of(
    value: object, tool: Callable[..., object], allowed: list[str]
) -> tuple[object, str | None]:
    """The value as plain fields, and the class a mock must name to build it again."""
    if is_model(value):
        dumped = cast(Any, value).model_dump(mode="json", by_alias=True, exclude_none=True)
    elif is_dataclass_object(value):
        dumped = dataclasses.asdict(cast(Any, value))
    elif is_named_tuple(value):
        dumped = cast(Any, value)._asdict()
    else:
        return plain(value, "returns"), None
    return plain(dumped, "returns"), class_path(type(value), tool, allowed)


def class_path(kind: type, tool: Callable[..., object], allowed: list[str]) -> str | None:
    """None when the tool's return annotation names the class, as build then finds it."""
    if mocks.return_type(tool) is kind:
        return None
    path = f"{kind.__module__}:{kind.__qualname__}"
    if not IMPORT_PATH.fullmatch(path):
        raise Unsavable(
            f"returns: {path} is not a class at the top of its module, so a mock cannot name it"
        )
    if not mocks.allowed(path, allowed):
        raise Unsavable(
            f"returns: {path} is outside the agent's packages ({', '.join(allowed) or 'none'}), "
            "so a mock cannot name it; give the tool a return annotation of that class"
        )
    return path


def differ(real: object, built: object, place: str) -> str | None:
    """The first place two values differ by type-strict recursive equality, or None.

    The types must be the same at every level, so True is not 1, 1 is not 1.0 and a list
    is not a tuple. Containers and classes with fields are compared part by part; anything
    else by ==.
    """
    if type(real) is not type(built):
        return f"{place}: {shown(real)}, got {shown(built)}"
    if isinstance(real, dict):
        a, b = cast(dict[object, object], real), cast(dict[object, object], built)
        if a.keys() != b.keys():
            gone = sorted(map(repr, a.keys() - b.keys())) or ["none"]
            extra = sorted(map(repr, b.keys() - a.keys())) or ["none"]
            return f"{place}: keys differ, missing {', '.join(gone)}, added {', '.join(extra)}"
        return next((d for k in a if (d := differ(a[k], b[k], f"{place}.{k}"))), None)
    if isinstance(real, list | tuple):
        a_items, b_items = cast(tuple[object, ...], real), cast(tuple[object, ...], built)
        if len(a_items) != len(b_items):
            return f"{place}: {len(a_items)} items, got {len(b_items)}"
        pairs = enumerate(zip(a_items, b_items, strict=True))
        return next((d for i, (x, y) in pairs if (d := differ(x, y, f"{place}[{i}]"))), None)
    if is_model(real):
        names = list(cast(dict[str, object], cast(Any, type(real)).model_fields))
        extra = (getattr(real, "model_extra", None), getattr(built, "model_extra", None))
        found = differ(extra[0], extra[1], f"{place}.(extra)") if any(extra) else None
        return found or next(
            (
                d
                for n in names
                if (d := differ(getattr(real, n), getattr(built, n), f"{place}.{n}"))
            ),
            None,
        )
    if is_dataclass_object(real):
        names = [f.name for f in dataclasses.fields(cast(Any, real))]
        return next(
            (
                d
                for n in names
                if (d := differ(getattr(real, n), getattr(built, n), f"{place}.{n}"))
            ),
            None,
        )
    if isinstance(real, float) and math.isnan(real) and math.isnan(cast(float, built)):
        return None
    return None if real == built else f"{place}: {shown(real)}, got {shown(built)}"


def secret_key(key: str) -> bool:
    """Whether a key names a secret by a whole word part, as a heuristic.

    access_token, apiKey and X-Auth-Token do; author, authority and max_tokens do not.
    """
    parts = [part.lower() for part in WORD.findall(key)]
    pairs = itertools.pairwise(parts)
    return any(part in SECRET_WORDS for part in parts) or any(p in SECRET_PAIRS for p in pairs)


def clean(value: object) -> tuple[object, list[Removed]]:
    """The value with secrets removed, and one entry per removal.

    Every string, keys included, goes through the existing patterns. A string value under a
    secret-like key is replaced whole, by the key-name heuristic. A number is never a secret,
    so token_count = 412 is kept.
    """
    removed: list[Removed] = []

    def text(found: str, key: str | None) -> str:
        kept, count = redacted(found)
        removed.extend({"key": key, "rule": PATTERN} for _ in range(count))
        return kept

    def walk(item: object, key: str | None) -> object:
        if isinstance(item, str):
            return text(item, key)
        if isinstance(item, list):
            return [walk(x, key) for x in cast(list[object], item)]
        if isinstance(item, dict):
            out: dict[str, object] = {}
            for name, inner in cast(dict[str, object], item).items():
                name = text(name, key)
                if isinstance(inner, str) and secret_key(name):
                    out[name] = REMOVED
                    removed.append({"key": name, "rule": KEY_NAME})
                else:
                    out[name] = walk(inner, name)
            return out
        return item

    return walk(value, None), removed


def unsaved(code: str, why: str) -> dict[str, object]:
    return {"code": code, "why": cleaned_text(why)}


def cleaned_text(text: str) -> str:
    return redacted(text)[0]


def converted(arguments: object) -> object | None:
    """A call's arguments as a recorded mock holds them, or None if no mock can."""
    try:
        return plain(arguments, "args")
    except Unsavable:
        return None


def saved_arguments(arguments: object) -> tuple[object, dict[str, object] | None]:
    """The arguments cleaned, or why a mock of the call cannot be written.

    A recorded mock matches its arguments exactly, so any removal means it could never
    match the real call again.
    """
    try:
        fields = plain(arguments, "args")
    except Unsavable as exc:
        return None, unsaved(UNSAVABLE, str(exc))
    cleaned, removed = clean(fields)
    if removed:
        where = ", ".join(sorted({str(r["key"]) for r in removed}))
        return None, unsaved(
            SECRET_CHANGED_SHAPE,
            f"its arguments hold a secret (under {where}), and a cleaned argument could "
            "never match the real call",
        )
    return cleaned, None


def tool_value(
    value: object,
    arguments: object,
    tool: Callable[..., object],
    shape: Callable[[object], object],
    allowed: list[str],
) -> dict[str, object]:
    """What a real tool call's line records: the value and arguments, or why not.

    Nothing here may fail the agent's call, and nothing reads a generator or a file.
    """
    try:
        fields, kind = fields_of(value, tool, allowed)
        built = shape(mocks.build(fields, kind, tool, allowed))
        where = differ(value, built, "returns")
    except Unsavable as exc:
        return unsaved(UNSAVABLE, str(exc))
    except Exception as exc:  # the team's class, its __eq__ or its validation
        return unsaved(UNSAVABLE, f"returns: a mock built from it fails: {kind_of(exc)}: {exc}")
    if where is not None:
        return unsaved(UNSAVABLE, where)
    args, problem = saved_arguments(arguments)
    if problem is not None:
        return problem
    cleaned, removed = clean(fields)
    if removed:
        try:
            shape(mocks.build(cleaned, kind, tool, allowed))
        except Exception as exc:
            return unsaved(
                SECRET_CHANGED_SHAPE,
                f"returns: with its secrets removed it no longer fits: {kind_of(exc)}: {exc}",
            )
    doc: dict[str, object] = {"value": cleaned, "args": args, "removed": removed}
    if kind is not None:
        doc["type"] = kind
    return doc


def mcp_returns(real: dict[str, object]) -> object:
    """What a mock's returns would be: the structured result, or its one text item."""
    if "structuredContent" in real:
        return real["structuredContent"]
    content = cast(list[dict[str, object]], real.get("content") or [])
    if len(content) != 1:
        raise Unsavable(f"result.content: {len(content)} items, and a mock gives one")
    if content[0].get("type") != "text":
        raise Unsavable(
            f"result.content[0]: a {content[0].get('type')} item, and a mock gives text"
        )
    return content[0].get("text")


def mcp_value(
    real: object, arguments: object, rebuild: Callable[[object], object]
) -> dict[str, object]:
    """What a real MCP call's line records. rebuild makes the result a mock of these returns
    would give, in its JSON form, as the client would read it."""
    if not isinstance(real, dict):
        return unsaved(UNSAVABLE, "result: the reply could not be read as JSON")
    result = cast(dict[str, object], real)
    if result.get("isError") is True:
        return unsaved(STATUS, "the result has isError, which is the server failing, as a 5xx is")
    try:
        fields = plain(mcp_returns(result), "returns")
        where = differ(result, rebuild(fields), "result")
    except Unsavable as exc:
        return unsaved(UNSAVABLE, str(exc))
    except Exception as exc:  # the library's own validation of the reply
        return unsaved(UNSAVABLE, f"result: a mock built from it fails: {kind_of(exc)}: {exc}")
    if where is not None:
        return unsaved(UNSAVABLE, where)
    args, problem = saved_arguments(arguments if arguments is not None else {})
    if problem is not None:
        return problem
    cleaned, removed = clean(fields)
    return {"value": cleaned, "args": args, "removed": removed}


def holds_in_toml(value: object) -> bool:
    """Whether every part of a JSON value has a TOML form: no null, no huge integer."""
    if value is None:
        return False
    if isinstance(value, bool | float | str):
        return True
    if isinstance(value, int):
        return INT64[0] <= value <= INT64[1]
    if isinstance(value, list):
        return all(map(holds_in_toml, cast(list[object], value)))
    return all(map(holds_in_toml, cast(dict[str, object], value).values()))


@dataclass(frozen=True)
class WebBody:
    """How a web reply's body is written: as json, as exact text, or not at all (code)."""

    form: str | None  # "json" or "text"; None when it cannot be written
    value: object
    removed: list[Removed] = field(default_factory=list[Removed])
    code: str | None = None


def web_body(body: str, removed_at_gate: list[Removed]) -> WebBody:
    """C.J3.12: the key-name rule runs on a JSON body first, then the form is chosen.

    body is the line's result, which the existing patterns cleaned at the gate; their
    removals come in removed_at_gate. A body that parses as JSON is cleaned by key name
    whatever its content type says, so a secret field is never kept by choosing text.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return WebBody("text", body, list(removed_at_gate))
    cleaned, removed = clean(parsed)
    removed = [*removed_at_gate, *removed]
    if holds_in_toml(cleaned):
        return WebBody("json", cleaned, removed)
    if removed:  # exact text cannot be cleaned and stay exact
        return WebBody(None, None, removed, SECRET_CHANGED_SHAPE)
    return WebBody("text", body)
