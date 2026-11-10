"""What a real call gave, saved only when a mock built from it gives the same back.

A recording run keeps, on the line of each real tool and MCP call, the value as plain
fields and the call's arguments, both cleaned of secrets. The test is the one the team
runs later: the gate builds the mock from the fields exactly as scrimmage test would, and
compares it with the real value by type-strict recursive equality. A value that does not
come back equal is listed, never written, so a mock can never give the agent a value of
another type. The original value goes back to the agent untouched.
"""

import dataclasses
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from typing import Any, cast

from scrimmage_core import mocks
from scrimmage_core.config import IMPORT_PATH
from scrimmage_core.errors import ScrimmageMockError
from scrimmage_core.redact import REMOVED, redacted

INT64 = (-(2**63), 2**63 - 1)  # TOML integers are 64 bits
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
# A pair written as one word, as in PRIVATEKEY or apikey.
SECRET_JOINED = frozenset(a + b for a, b in SECRET_PAIRS)
# A secret word followed by one of these names something about the secret, not the secret:
# session_id, token_count, auth_type. A session or paging tool takes these as arguments, and
# any removal from arguments means the call can never be recorded.
ABOUT = frozenset({"id", "count", "total", "type", "mode", "used", "left", "remaining", "limit"})
# A secret word after one of these counts it: max_tokens, num_tokens.
COUNTING = frozenset({"max", "min", "num", "total"})
# A token after one of these is a cursor for the next page, not a credential: page_token,
# next_page_token, next_token (AWS's NextToken), continuation_token.
PAGING = frozenset({"page", "next", "continuation", "pagination", "sync", "cursor"})
# Word parts: a run of capitals before a capitalised word (API in APIKey), a word, or a run
# of capitals. Anything that is not a letter, such as _, -, ., a space or a digit, splits.
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
KEY_NAME, PATTERN = "key_name", "pattern"
UNSAVABLE, SECRET_CHANGED_SHAPE, STATUS = "unsavable", "secret_changed_shape", "status"
# Scrimmage itself failed while recording: a bug, never the team's value. J3b2 exits 3 on it.
SCRIMMAGE_ERROR = "scrimmage_error"
HERE = os.path.dirname(os.path.abspath(__file__))

Removed = dict[str, object]


class Unsavable(Exception):
    """A value or argument a mock cannot hold, with the place that is the problem."""


def kind_of(value: object) -> str:
    """A value's type as a person reads it: str, int, a class's own name."""
    return type(value).__name__


def text_problem(text: str) -> str | None:
    """Why a text cannot be saved: a lone surrogate, such as a file name's undecodable byte,
    has no UTF-8 form, so neither JSON nor TOML can hold it."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "a text with a lone surrogate, which has no UTF-8 form"
    return None


def plain(value: object, place: str) -> object:
    """A copy made only of what TOML and JSON hold, read by exact type.

    A subclass, such as a StrEnum, an IntEnum or a defaultdict, is not its base: the mock
    would give back the base, so it raises Unsavable. A tuple becomes a list; whether the
    tool gives back a tuple is for the round trip to decide. A value that holds itself, or
    is nested too deep for Python's stack, is Unsavable too.
    """
    try:
        return walk_plain(value, place)
    except RecursionError:
        raise Unsavable(f"{place}: nested too deep, or holds itself") from None


def walk_plain(value: object, place: str) -> object:
    kind = type(value)
    if kind is int:
        if not INT64[0] <= cast(int, value) <= INT64[1]:
            raise Unsavable(f"{place}: an integer beyond 64 bits, which TOML cannot hold")
        return value
    if kind is str and (why := text_problem(cast(str, value))):
        raise Unsavable(f"{place}: {why}")
    if value is None or kind in (str, bool, float):
        return value
    if kind is list or kind is tuple:
        items = cast(list[object], value)
        return [walk_plain(item, f"{place}[{i}]") for i, item in enumerate(items)]
    if kind is dict:
        out: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise Unsavable(f"{place}: a key that is not text, a {kind_of(key)}")
            if why := text_problem(key):
                raise Unsavable(f"{place}: a key that is {why}")
            out[key] = walk_plain(item, f"{place}.{key}")
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
    else by ==. The message names places and types, never a value, as a value can be a
    secret and the message goes on the record line.
    """
    if type(real) is not type(built):
        return f"{place}: {kind_of(real)}, got {kind_of(built)}"
    if isinstance(real, dict):
        a, b = cast(dict[object, object], real), cast(dict[object, object], built)
        if a.keys() != b.keys():
            gone, extra = len(a.keys() - b.keys()), len(b.keys() - a.keys())
            return f"{place}: keys differ, {gone} missing and {extra} added"
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
    try:
        equal = bool(real == built)
    except Exception as exc:  # an __eq__ or __bool__ written in C has no Python frame of its own
        raise Unsavable(
            f"{place}: comparing the {kind_of(real)} fails with {kind_of(exc)}"
        ) from None
    return None if equal else f"{place}: the {kind_of(real)} differs"


# Words a final s is dropped from, so tokens is token and api_keys holds the pair api key.
PLURALS = SECRET_WORDS | SECRET_JOINED | {word for pair in SECRET_PAIRS for word in pair}


def singular(part: str) -> str:
    """A word part in lower case, without the s of a plural, so access stays access."""
    part = part.lower()
    return part[:-1] if part.endswith("s") and part[:-1] in PLURALS else part


def secret_key(key: str) -> bool:
    """Whether a key names a secret by a whole word part, as a heuristic.

    access_token, apiKey, refresh_tokens and PRIVATEKEY do; author, max_tokens, session_id,
    token_count and next_page_token do not. A plural matches as its singular.
    """
    parts = ["", *map(singular, WORD.findall(key)), "", ""]
    for i in range(1, len(parts) - 2):
        last, part, then, later = parts[i - 1 : i + 3]
        word = part in SECRET_WORDS or part in SECRET_JOINED
        if (
            word
            and (then not in ABOUT or (part == "secret" and then == "id"))  # Vault's AppRole
            and last not in COUNTING
            and not (part == "token" and last in PAGING)
        ):
            return True
        if (part, then) in SECRET_PAIRS and later not in ABOUT:  # access_key_id is an id
            return True
    return False


def clean(value: object) -> tuple[object, list[Removed]]:
    """The value with secrets removed, and one entry per removal.

    Every string, keys included, goes through the existing patterns. A string under a
    secret-like key, or anywhere in a list under one, is replaced whole, by the key-name
    heuristic. A number is never a secret, so token_count = 412 is kept.
    """
    removed: list[Removed] = []

    def text(found: str, key: str | None) -> str:
        kept, count = redacted(found)
        removed.extend({"key": key, "rule": PATTERN} for _ in range(count))
        return kept

    def walk(item: object, key: str | None, secret: bool) -> object:
        if isinstance(item, str):
            if secret:
                removed.append({"key": key, "rule": KEY_NAME})
                return REMOVED
            return text(item, key)
        if isinstance(item, list):
            return [walk(x, key, secret) for x in cast(list[object], item)]
        if isinstance(item, dict):
            out: dict[str, object] = {}
            for name, inner in cast(dict[str, object], item).items():
                name = text(name, key)
                # A table under a secret key is secret all the way down; a number never is.
                out[name] = walk(inner, name, secret or secret_key(name))
            return out
        return item

    return walk(value, None, False), removed


def embedded_secret(value: object) -> str | None:
    """Where a string holds JSON with a secret by its key's name, or None.

    A tool that returns json.dumps(reply) holds the reply in one string, which the key rule
    cannot see. A string is kept exactly, so a secret in it cannot be taken out.
    """
    if isinstance(value, list):
        return next((w for item in cast(list[object], value) if (w := embedded_secret(item))), None)
    if isinstance(value, dict):
        items = cast(dict[str, object], value).values()
        return next((w for item in items if (w := embedded_secret(item))), None)
    if not isinstance(value, str) or value.lstrip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict | list):
        return None
    _, removed = clean(cast(object, parsed))
    if removed:
        return ", ".join(sorted({str(r["key"]) for r in removed}))
    return embedded_secret(cast(object, parsed))


def json_text_problem(value: object, place: str) -> dict[str, object] | None:
    """Why a value with JSON text inside it cannot be written, if a secret is in that text."""
    where = embedded_secret(value)
    if where is None:
        return None
    return unsaved(
        SECRET_CHANGED_SHAPE,
        f"{place}: a JSON text inside it holds a secret (under {where}), and an exact text "
        "cannot be cleaned",
    )


def ours(exc: BaseException) -> bool:
    """Whether an error was raised in Scrimmage's own code, not the team's: the frame that
    raised it is in this package. ScrimmageMockError is Scrimmage saying the mock does not fit
    the team's type, so it is not a bug."""
    if isinstance(exc, ScrimmageMockError):
        return False
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return (
        tb is not None and os.path.dirname(os.path.abspath(tb.tb_frame.f_code.co_filename)) == HERE
    )


def scrimmage_error(exc: BaseException) -> dict[str, object]:
    """A bug in Scrimmage while it recorded a call: listed apart from a value no mock can hold."""
    return unsaved(SCRIMMAGE_ERROR, f"Scrimmage failed while recording it: {kind_of(exc)}")


def unsaved(code: str, why: str) -> dict[str, object]:
    return {"code": code, "why": cleaned_text(why)}


def cleaned_text(text: str) -> str:
    return redacted(text)[0]


SHOWN_CAP = 200  # a miss line shows this much of a call's arguments


def without_json_secrets(value: object) -> object:
    """Every string that holds JSON with a secret by its key's name, replaced whole."""
    if isinstance(value, str):
        return REMOVED if embedded_secret(value) else value
    if isinstance(value, list):
        return [without_json_secrets(x) for x in cast(list[object], value)]
    if isinstance(value, dict):
        return {k: without_json_secrets(v) for k, v in cast(dict[str, object], value).items()}
    return value


def shown_args(value: object, differing: Sequence[str]) -> str | None:
    """A call's arguments as display text for a miss, or None if they are not a table.

    The values of the keys in differing are shown, cleaned by a heuristic: the patterns, the
    key-name rule, and a string of JSON with a secret by its key's name replaced whole. Every
    other key shows its name and type only, as <str>, because a key that matched has no reason
    to print. The differing keys come first, and the text is cut last, after the cleaning.
    """
    if not isinstance(value, dict):
        return None
    table = cast(dict[str, object], value)
    order = [k for k in differing if k in table] + [k for k in table if k not in differing]
    parts: list[str] = []
    for key in order:
        name = cleaned_text(key)
        held = table[key]
        if key in differing and secret_key(key) and isinstance(held, int | float):
            # clean keeps a number, as it is never a secret pattern, but a PIN under a
            # secret's name would print to a CI log here.
            shown = f"<{type(held).__name__}>"
        elif key in differing:
            cleaned, _ = clean({key: table[key]})
            shown = json.dumps(without_json_secrets(cast(dict[str, object], cleaned)[name]))
        else:
            shown = f"<{type(table[key]).__name__}>"
        parts.append(f"{json.dumps(name)}: {shown}")
    return ("{" + ", ".join(parts) + "}")[:SHOWN_CAP]


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
    problem = json_text_problem(cleaned, "args")
    return (None, problem) if problem else (cleaned, None)


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
    except Exception as exc:  # the team's class, its __eq__ or its validation, or Scrimmage's bug
        if ours(exc):
            return scrimmage_error(exc)
        # The error's type only: its message, such as pydantic's, can quote the value.
        return unsaved(UNSAVABLE, f"returns: a mock built from it fails with {kind_of(exc)}")
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
                f"returns: with its secrets removed it no longer fits, {kind_of(exc)}",
            )
    embedded = json_text_problem(cleaned, "returns")
    if embedded is not None:
        return embedded
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


def failed_result(real: object) -> dict[str, object] | None:
    """Why a result is never saved before anything else is tried: it is no JSON, or the
    server failed, as isError says, which a mock must not copy, as a 5xx is not."""
    if not isinstance(real, dict):
        return unsaved(UNSAVABLE, "result: the reply could not be read as JSON")
    if cast(dict[str, object], real).get("isError") is True:
        return unsaved(STATUS, "the result has isError, which is the server failing, as a 5xx is")
    return None


def mcp_value(
    real: object, arguments: object, rebuild: Callable[[object], object]
) -> dict[str, object]:
    """What a real MCP call's line records. rebuild makes the result a mock of these returns
    would give, in its JSON form, as the client would read it."""
    failed = failed_result(real)
    if failed is not None:
        return failed
    result = cast(dict[str, object], real)
    try:
        fields = plain(mcp_returns(result), "returns")
        where = differ(result, rebuild(fields), "result")
    except Unsavable as exc:
        return unsaved(UNSAVABLE, str(exc))
    except Exception as exc:  # the library's own validation of the reply, or Scrimmage's bug
        if ours(exc):
            return scrimmage_error(exc)
        return unsaved(UNSAVABLE, f"result: a mock built from it fails with {kind_of(exc)}")
    if where is not None:
        return unsaved(UNSAVABLE, where)
    args, problem = saved_arguments(arguments if arguments is not None else {})
    if problem is not None:
        return problem
    cleaned, removed = clean(fields)
    if removed:
        try:
            rebuild(cleaned)
        except Exception as exc:
            return unsaved(
                SECRET_CHANGED_SHAPE,
                f"result: with its secrets removed it no longer fits, {kind_of(exc)}",
            )
    embedded = json_text_problem(cleaned, "result")
    if embedded is not None:
        return embedded
    return {"value": cleaned, "args": args, "removed": removed}
