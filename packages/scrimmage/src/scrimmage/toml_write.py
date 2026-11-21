"""A TOML writer for the mocks scrimmage record writes, and the read-back that proves it.

Python has no TOML writer, and the SDK imports only the standard library, so this is
Scrimmage's own. It escapes everything: a value from a real reply is data, and nothing in
it can end a string, a comment or a table, or add a fault. It writes only TOML 1.0, as
tomllib on Python 3.11 reads no more.
"""

import math
import re
import tomllib
from collections.abc import Sequence
from typing import cast

from scrimmage import capture
from scrimmage.config import literal

LINE = 100  # an inline list or table this long or longer is written over several lines
INDENT = "    "
BARE = re.compile(r"[A-Za-z0-9_-]+")
# The tables written as their own section when they do not fit on one line.
SECTIONS = ("args", "returns", "json")
ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


class Unwritable(ValueError):
    """A value TOML cannot hold, such as None or a lone surrogate."""


class WriterBug(RuntimeError):
    """What was written does not read back as what was meant: a bug in Scrimmage, exit 3."""


def string(text: str, *, value: bool = True) -> str:
    """A basic string. Every control character is escaped, and no string spans lines.

    value: a value, which the file reads ${NAME} in, so a recorded ${ is written as the
    literal $${ and is never a placeholder by accident. A key is read as it is written."""
    out = ['"']
    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise Unwritable("a lone surrogate, which is not text TOML can hold")
        if char in ESCAPES:
            out.append(ESCAPES[char])
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    out.append('"')
    written = "".join(out)
    return written.replace("${", "$${") if value else written


def key(name: str) -> str:
    return name if BARE.fullmatch(name) else string(name, value=False)


def inline(value: object) -> str:
    """A value on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not capture.INT64[0] <= value <= capture.INT64[1]:
            raise Unwritable("an integer beyond 64 bits")
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return string(value)
    if isinstance(value, list):
        return "[" + ", ".join(inline(item) for item in cast(list[object], value)) + "]"
    if isinstance(value, dict):
        table = cast(dict[str, object], value)
        pairs = [f"{key(k)} = {inline(v)}" for k, v in table.items()]
        return "{" + ", ".join(pairs) + "}" if pairs else "{}"
    if value is None:
        raise Unwritable("None, which TOML has no form for")
    raise Unwritable(f"a {type(value).__name__}")


def assignment(name: str, value: object, width: int = 0) -> list[str]:
    """name = value, on one line, or a long list with one item per line.

    A table inside a list stays inline, as TOML 1.0 has no multi-line inline table.
    """
    head = f"{key(name).ljust(width)} = "
    text = inline(value)
    if not isinstance(value, list) or len(head) + len(text) < LINE:
        return [head + text]
    items = cast(list[object], value)
    return [head + "[", *(f"{INDENT}{inline(item)}," for item in items), "]"]


def section(name: str, value: object, text: str) -> bool:
    """Whether a top table is written as its own [test.mock.<name>] section."""
    return name in SECTIONS and isinstance(value, dict) and len(f"{name} = {text}") >= LINE


def mock(fields: dict[str, object], comments: Sequence[str] = ()) -> str:
    """One [[test.mock]] block, its comments first, read back to prove it.

    fields is the mock as config reads it: call, recorded, args, returns and so on. A long
    args, returns or json table goes after the other keys as its own section.
    """
    inline_keys = [k for k in fields if not section(k, fields[k], inline(fields[k]))]
    width = max((len(key(k)) for k in inline_keys), default=0)
    lines = [comment(text) for text in comments]
    lines.append("[[test.mock]]")
    for name in inline_keys:
        lines += assignment(name, fields[name], width)
    for name in fields:
        if name in inline_keys:
            continue
        table = cast(dict[str, object], fields[name])
        lines.append(f"[test.mock.{name}]")
        inner = max((len(key(k)) for k in table), default=0)
        for item, value in table.items():
            lines += assignment(item, value, inner)
    block = "\n".join(lines) + "\n"
    read_back(block, fields)
    return block


def read_back(block: str, fields: dict[str, object]) -> None:
    """The block, read by tomllib, must be exactly the mock meant, or it is Scrimmage's bug."""
    try:
        found = literal(tomllib.loads(block))  # $${ is ${ once read; nothing is filled in
    except tomllib.TOMLDecodeError as exc:
        raise WriterBug(f"scrimmage wrote a mock tomllib cannot read: {exc}") from exc
    where = capture.differ({"test": {"mock": [fields]}}, found, "mock")
    if where is not None:
        raise WriterBug(f"scrimmage wrote a mock that reads back differently: {where}")


def comment(text: str) -> str:
    """One comment line, whatever the text holds: anything unprintable is escaped as repr
    escapes it, so a newline in a reply cannot end the comment."""
    escaped = "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)
    return f"# {escaped}".rstrip()
