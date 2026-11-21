"""The change an append or replace fault makes to a result, as pure functions.

No I/O and no state. A text takes append and replace. Structured data (a dict, a list, a
tuple, or text that is a JSON object or array) takes only replace with find, on string
values, never on keys. A value that cannot take the change comes back unchanged with the
reason, so the gate can say why on the line. Redaction comes first: a change that lands on
text the record would remove is not made, because the line could then not show what the
agent saw.
"""

import copy
import functools
import importlib
import inspect
import json
import re
import sys
import types
import typing
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from scrimmage import provider_openai as openai_table
from scrimmage.config import Fault
from scrimmage.redact import redact, spans

MAX_DEPTH = 100  # nesting deeper than this is not changed, so no value can overflow the stack
SHOWN_PARTS = 5
PART_CUT = 80

Kind = Literal["shape", "data", "declared", "span", "schema", "unsupported"]


@dataclass(frozen=True)
class Changed:
    """The new value, how many places changed, and where (up to five, as paths)."""

    value: object
    count: int
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Unchangeable:
    """Why a value was left as it was.

    shape: the value's type has no place for this change. data: the type has, this value does
    not, such as a find that is not in it. declared: the declared type rules out every call.
    span: the change would land on text the record removes, naming the rule. schema: the
    changed structured content breaks the tool's output schema; why is the whole message.
    unsupported: this kind of call can never take the change, such as a streamed model reply;
    why is the whole message.
    """

    why: str
    kind: Kind = "shape"
    rule: str | None = None

    @property
    def mistake(self) -> bool:
        """Whether this is a mistake in the test at once, not a call left out of the row."""
        return self.kind in ("declared", "span", "schema", "unsupported")

    def message(self, fault: Fault, label: str) -> str | None:
        """The mistake as the team reads it, naming the fix. None when it is no mistake."""
        if self.kind == "span":
            return (
                f"{fault.do} on {label} would change text the {self.rule} rule removes from the "
                "record, so the line could not show what changed; choose a find away from it"
            )
        if self.kind == "declared":
            return f"{label} {self.why}"
        return self.why if self.kind in ("schema", "unsupported") else None


Result = Changed | Unchangeable


def plain(value: object, depth: int = 0, seen: frozenset[int] = frozenset()) -> str | None:
    """None if the value is plain data: str, int, float, bool, None, and dict, list and tuple
    of those, exactly these types. Else why not."""
    kind = type(value)
    if kind in (str, int, float, bool, type(None)):
        return None
    if kind not in (dict, list, tuple):
        return f"it holds a {kind.__name__}"
    if depth >= MAX_DEPTH:
        return f"it is nested deeper than {MAX_DEPTH} levels"
    if id(value) in seen:
        return "it holds itself"
    inside = seen | {id(value)}
    held = cast("dict[object, object]", value).values() if kind is dict else value
    for member in cast("Sequence[object]", list(cast("Sequence[object]", held))):
        if (why := plain(member, depth + 1, inside)) is not None:
            return why
    return None


def places(text: str, find: str) -> list[int]:
    """Where find starts in text, each place after the end of the one before, as replace does."""
    found: list[int] = []
    at = text.find(find)
    while at != -1:
        found.append(at)
        at = text.find(find, at + len(find))
    return found


def spans_clash(real: str, edits: list[tuple[int, int, str]]) -> str | None:
    """The rule of a redaction span the edits touch or make, if any. The edits are
    (start, end, new text) in the real text, none overlapping, in order.

    An edit that overlaps a span of the real text, or an insertion inside one, is a clash.
    Then the rules are run on the result: its spans must be exactly the real text's spans,
    moved, and the new texts' own. Any other span is the joint of an append, or of a find
    next to a key, which made one out of two harmless pieces.
    """
    before = spans(real)
    for start, end, _ in edits:
        for s, e, rule in before:
            if (start < e and s < end) or (start == end and s < start < e):
                return rule
    expected: list[tuple[int, int]] = []
    moved = 0  # how far the text has moved by the edits before this place
    inserted: list[tuple[int, int]] = []
    for start, end, new in edits:
        inserted += [(start + moved + s, start + moved + e) for s, e, _ in spans(new)]
        moved += len(new) - (end - start)
    for s, e, _ in before:
        shift = sum(len(new) - (end - start) for start, end, new in edits if end <= s)
        expected.append((s + shift, e + shift))
    found = spans(apply(real, edits))
    if sorted([*expected, *inserted]) == sorted((s, e) for s, e, _ in found):
        return None
    return found[0][2] if found else before[0][2]


def apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    out: list[str] = []
    at = 0
    for start, end, new in edits:
        out += [text[at:start], new]
        at = end
    out.append(text[at:])
    return "".join(out)


def json_object(text: str) -> dict[str, object] | list[object] | None:
    """The parsed JSON when the text is an object or an array. Other JSON, such as a number,
    stays text."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return cast("list[object]", value) if isinstance(value, list) else None


def change_text(text: str, fault: Fault, where: str = "result", *, sniff: bool = True) -> Result:
    """Change a text. A text that is a JSON object or array is changed the JSON way: append
    cannot, and replace with find changes string values."""
    injected = fault.text or ""
    if sniff and (parsed := json_object(text)) is not None:
        if fault.do == "append":
            return Unchangeable("the text is JSON, which append would break; use replace with find")
        if fault.find is not None:
            return change_json_text(text, parsed, fault, where)
    if fault.do == "append":
        edits = [(len(text), len(text), injected)]
        count = 1
    elif fault.find is None:
        edits = [(0, len(text), injected)]
        count = 1
    else:
        found = places(text, fault.find)
        if not found:
            return Unchangeable("find is not in the result", "data")
        edits = [(at, at + len(fault.find), injected) for at in found]
        count = len(found)
    # A whole replace keeps nothing of the real text, so it has no span to land on.
    whole = fault.do == "replace" and fault.find is None
    if not whole and (rule := spans_clash(text, edits)) is not None:
        return Unchangeable(f"the change touches text the {rule} rule removes", "span", rule)
    return Changed(apply(text, edits), count, (where,))


def dumped(value: object, text: str) -> str | None:
    """The JSON back as text, written as the original was: indented if it was, else compact.
    None if it cannot be written, such as NaN."""
    try:
        if "\n" in text.strip():
            return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, RecursionError):
        return None


def change_json_text(text: str, parsed: object, fault: Fault, where: str) -> Result:
    """Replace with find inside the string values of JSON text, and write it back."""
    done = change_data(parsed, fault, "$")
    if isinstance(done, Unchangeable):
        return done
    written = dumped(done.value, text)
    if written is None:
        return Unchangeable("the JSON holds NaN or Infinity, which cannot be written back", "data")
    parts = tuple(f"{where}{part.removeprefix('$')}" for part in done.parts)
    return Changed(written, done.count, parts)


def change_data(value: object, fault: Fault, root: str = "$") -> Result:
    """Replace with find in every string value, at any depth, never in a key. The result is a
    new copy in which each container keeps its type; the value itself is never changed."""
    if fault.do != "replace" or fault.find is None:
        return Unchangeable(
            f"the result is {type(value).__name__} data, which {fault.do} cannot change; "
            "use replace with find"
        )
    if (why := plain(value)) is not None:
        return Unchangeable(f"the result cannot be changed: {why}")
    find, injected = fault.find, fault.text or ""
    found: list[tuple[str, int]] = []  # path and count, in order
    clash: list[str] = []

    def walk(item: object, path: str) -> object:
        kind = type(item)
        if kind is str:
            text = cast(str, item)
            at = places(text, find)
            if not at:
                return text
            edits = [(a, a + len(find), injected) for a in at]
            if rule := spans_clash(text, edits):
                clash.append(rule)
                return text
            found.append((path, len(at)))
            return apply(text, edits)
        if kind is dict:
            table = cast("dict[object, object]", item)
            return {key: walk(member, f"{path}.{key}") for key, member in table.items()}
        if kind is list:
            return [
                walk(member, f"{path}[{i}]") for i, member in enumerate(cast("list[object]", item))
            ]
        if kind is tuple:
            return tuple(
                walk(member, f"{path}[{i}]")
                for i, member in enumerate(cast("tuple[object, ...]", item))
            )
        return item

    new = walk(value, root)
    if clash:
        return Unchangeable(
            f"the change touches text the {clash[0]} rule removes", "span", clash[0]
        )
    if not found:
        return Unchangeable("find is not in the result", "data")
    parts = tuple(redact(path)[:PART_CUT] for path, _ in found[:SHOWN_PARTS])
    return Changed(new, sum(count for _, count in found), parts)


NONE = type(None)
STRUCTURED = (dict, list, tuple, Mapping, Sequence, set, frozenset)


def structured(origin: object) -> bool:
    """A declared type that is a container of data: not text, not bytes."""
    try:
        return (
            isinstance(origin, type)
            and issubclass(origin, STRUCTURED)
            and not issubclass(origin, str | bytes | bytearray)
        )
    except TypeError:
        return False


def members(declared: object) -> list[object]:
    """The types a declared return type allows, a union taken apart."""
    origin = typing.get_origin(declared)
    if origin is typing.Union or origin is types.UnionType:
        return [m for arg in typing.get_args(declared) for m in members(arg)]
    return [declared]


def can_take(declared: object, fault: Fault) -> bool | None:
    """Whether a declared return type can take this fault: True, False, or None when it
    says nothing (no annotation, Any, object, a type variable, or a type it cannot read)."""
    if declared is None or declared is typing.Any or declared is object:
        return None
    if isinstance(declared, Unresolved):
        return None
    text_ok = fault.do == "append" or fault.find is None
    any_unknown = False
    for member in members(declared):
        origin = typing.get_origin(member) or member
        if member is None or member is NONE:
            continue
        if member is typing.Any or member is object or isinstance(member, typing.TypeVar):
            any_unknown = True
        elif origin is str:
            return True
        elif structured(origin):
            if not text_ok:
                return True
        elif not isinstance(origin, type):
            any_unknown = True  # a form it does not read, such as Literal or Annotated
    return None if any_unknown else False


@dataclass(frozen=True)
class Unresolved:
    """A return annotation that is written but cannot be resolved at run time, such as a
    name imported only under TYPE_CHECKING. text is the annotation as written."""

    text: str


def annotated(function: object) -> object:
    """What holds the annotations of a callable: a partial's function, a method's function,
    the function an object made by functools.update_wrapper wraps, or a callable object's
    __call__. A function made by functools.wraps holds them itself, copied."""
    while isinstance(function, functools.partial):
        function = cast("functools.partial[object]", function).func
    function = getattr(function, "__func__", function)  # a bound method
    if not (inspect.isfunction(function) or isinstance(function, type)) and callable(function):
        if hasattr(function, "__wrapped__"):
            return unwrapped(function)
        function = getattr(type(function).__call__, "__func__", type(function).__call__)
    return function


def unwrapped(function: object) -> object:
    """The function at the end of a chain of __wrapped__, whose globals name what the team's
    module imported. A wrapper's are its decorator's module's, so typing reads these too."""
    try:
        return inspect.unwrap(function)  # pyright: ignore[reportArgumentType]
    except ValueError:  # a chain that loops
        return function


def annotations_of(function: object, string: bool = False) -> dict[str, object]:
    """The function's annotations, none evaluated. On Python 3.14 they are lazy: FORWARDREF
    keeps a name it cannot resolve as a reference, and STRING gives each as written."""
    if sys.version_info >= (3, 14):
        import annotationlib

        kind = annotationlib.Format.STRING if string else annotationlib.Format.FORWARDREF
        return dict(annotationlib.get_annotations(function, format=kind))  # pyright: ignore
    return dict(inspect.get_annotations(function))  # pyright: ignore[reportArgumentType]


def return_annotation(function: object) -> tuple[bool, object]:
    """Whether the function writes a return annotation, and the annotation as written. Other
    annotations are never evaluated, so a parameter type that cannot be resolved is no matter."""
    try:
        written = annotations_of(function)
    except Exception:  # on 3.14, an annotation that raises, such as a: 1/0
        if sys.version_info < (3, 14):
            raise
        written = annotations_of(function, string=True)  # raises too, for some
    return "return" in written, written.get("return")


def written_text(function: object, written: object) -> str:
    """A return annotation as the team wrote it, with one layer of quotes taken off."""
    try:  # on 3.14 as written in the source; before it, the annotation as stored
        text = annotations_of(function, string=True).get("return")
    except Exception:  # an object with no annotations of its own
        text = None
    if not isinstance(text, str):
        reference = getattr(written, "__forward_arg__", None)
        text = written if isinstance(written, str) else str(reference or written)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def declared_return(function: object) -> object:
    """The return annotation of a function, resolved: None when there is none, Unresolved
    when it is written but cannot be resolved."""
    target = annotated(function)
    try:
        found, written = return_annotation(target)
    except Exception as exc:
        if not inspect.isfunction(target):
            return None  # not a function, class or module, so it has no annotations
        # On 3.14 annotations are read all at once, and one that raises hides the rest.
        return Unresolved(f"annotations that raise {type(exc).__name__}")
    if not found:
        return None
    # A stand-in that holds only the return annotation, with the function's own globals, so
    # typing resolves it, nested forward references and Annotated included, and nothing else.
    space = cast("dict[str, object]", getattr(unwrapped(target), "__globals__", None) or {})
    stand_in = types.FunctionType(borrowed.__code__, space)
    stand_in.__annotations__ = {"return": written}
    try:
        return typing.get_type_hints(stand_in)["return"]
    except Exception:  # a name it cannot find, or text that is no type
        pass
    try:
        return Unresolved(written_text(target, written))
    except Exception as exc:  # the team's own type, whose __getattr__ or __str__ raises
        return Unresolved(f"annotations that raise {type(exc).__name__}")


def borrowed() -> None:
    """The code a stand-in function borrows; it never runs."""


def change(value: object, fault: Fault, declared: object = None, where: str = "result") -> Result:
    """Change a tool's return value. The declared return type, if it says anything, can rule
    the fault out for every call at once; else the value's own type decides."""
    if can_take(declared, fault) is False:
        shown = getattr(declared, "__name__", None) or str(declared)
        what = "text" if fault.do == "append" or fault.find is None else "text or data"
        return Unchangeable(
            f"is declared to return {shown}, not {what}, so {fault.do} has no place to put "
            f"text; fault the web request the tool makes, return {what}, or choose another fault",
            "declared",
        )
    if value is None:
        return Unchangeable("the tool returned None")
    if type(value) is str:
        return change_text(value, fault, where)
    if type(value) in (dict, list, tuple):
        return change_data(value, fault, "$")
    return Unchangeable(f"the tool returned a {type(value).__name__}, which has no text to change")


def text_items(raw: dict[str, object]) -> list[int]:
    """The places of the text items of an MCP result's content."""
    content = raw.get("content")
    if not isinstance(content, list):
        return []
    return [
        i
        for i, item in enumerate(cast("list[object]", content))
        if isinstance(item, dict)
        and cast("dict[str, object]", item).get("type") == "text"
        and isinstance(cast("dict[str, object]", item).get("text"), str)
    ]


def change_mcp_result(raw: dict[str, object], fault: Fault) -> Result:
    """Change an MCP tool result as a server sends it, on its text items and its structured
    content. An agent may read either, so with both, a find must change both to count.

    append: the last text item, unless it is JSON or there is structured content. replace with
    no find: every text item becomes one item holding the text, other items kept; with
    structured content it cannot. replace with find: every text item, the JSON way for a JSON
    one, and every string value of the structured content.
    """
    if raw.get("isError") is True:
        return Unchangeable("the result is an error result", "data")
    structured = raw.get("structuredContent")
    has_structured = isinstance(structured, dict)
    places_ = text_items(raw)
    if not places_ and not has_structured:
        return Unchangeable("the result has no text and no structured content")
    new = copy.deepcopy(raw)
    items = cast("list[dict[str, object]]", new.get("content") or [])
    if fault.do == "append" or fault.find is None:
        if has_structured:
            return Unchangeable(
                "the result has structured content, which "
                f"{fault.do} cannot change; use replace with find"
            )
        if fault.do == "append":
            i = places_[-1]
            done = change_text(str(items[i]["text"]), fault, f"content[{i}].text")
            if isinstance(done, Unchangeable):
                return done
            items[i]["text"] = done.value
            return Changed(new, 1, done.parts)
        first = places_[0]
        items[first]["text"] = fault.text or ""
        for i in reversed(places_[1:]):
            del items[i]
        return Changed(
            new, len(places_), tuple(f"content[{i}].text" for i in places_[:SHOWN_PARTS])
        )
    count = 0
    parts: list[str] = []
    in_content = False
    for i in places_:
        done = change_text(str(items[i]["text"]), fault, f"content[{i}].text")
        if isinstance(done, Changed):
            items[i]["text"] = done.value
            count, parts, in_content = count + done.count, [*parts, *done.parts], True
        elif done.kind == "span":
            return done
    in_structured = False
    if has_structured:
        done = change_data(cast("dict[str, object]", structured), fault, "structuredContent")
        if isinstance(done, Changed):
            new["structuredContent"] = done.value
            count, parts, in_structured = count + done.count, [*parts, *done.parts], True
        elif done.kind == "span":
            return done
    if has_structured and places_:
        if in_structured and not in_content:
            return Unchangeable("find is only in structuredContent, not in content", "data")
        if in_content and not in_structured:
            return Unchangeable("find is only in content, not in structuredContent", "data")
    if not in_content and not in_structured:
        return Unchangeable("find is not in the result", "data")
    return Changed(new, count, tuple(parts[:SHOWN_PARTS]))


WEB_CAP = 10 * 1024 * 1024  # a body over this is not read to be changed
PACKED = ("", "identity", "gzip", "deflate", "br")  # content-encodings a body is read through
NO_BROTLI = (
    "the reply is encoded with br, and neither brotli nor brotlicffi 1.2 or later is installed "
    "to unpack it"
)
BODY_HEADERS = ("content-length", "content-encoding", "transfer-encoding")


@dataclass(frozen=True)
class WebChanged:
    """A changed reply: the body, the headers it goes with, what changed, and the status
    when the agent gets another than the real one."""

    body: bytes
    headers: list[tuple[str, str]]
    count: int
    parts: tuple[str, ...]
    status: int | None = None


def header(headers: Sequence[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def media_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def charset_of(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"') or None
    return None


def with_charset(content_type: str, charset: str) -> str:
    """The content type with its charset set; added if it named none."""
    kept = [content_type.split(";")[0].strip()]
    kept += [
        part.strip()
        for part in content_type.split(";")[1:]
        if part.partition("=")[0].strip().lower() != "charset"
    ]
    return "; ".join([*kept, f"charset={charset}"])


def web_kind(status: int, method: str, headers: Sequence[tuple[str, str]]) -> str | Unchangeable:
    """ "text" or "json" when the reply's headers allow a change, else why not. Decided by the
    headers alone, so a stream is never read to find out."""
    if method.upper() == "HEAD":
        return Unchangeable("the request was a HEAD, which has no body", "data")
    if not 200 <= status < 300 or status == 204:
        return Unchangeable(f"the reply status is {status}, which is not changed", "data")
    packed = packed_why(header(headers, "content-encoding") or "")
    if packed is not None:
        return Unchangeable(packed)
    declared = header(headers, "content-type")
    if declared is None:
        return Unchangeable("the reply has no content-type, so its body is not read as text")
    kind = media_type(declared)
    if kind == "application/json" or (kind.startswith("application/") and kind.endswith("+json")):
        return "json"
    if kind.startswith("text/") and kind != "text/event-stream":
        return "text"
    return Unchangeable(f"the reply is {kind}, which is not text or JSON")


def packed_why(encoding: str) -> str | None:
    """None when a body in this content-encoding can be read, else why not."""
    encoding = encoding.strip().lower()
    if encoding not in PACKED:
        return f"the reply is encoded with {encoding}, which is not changed"
    if encoding == "br" and brotli_module() is None:
        return NO_BROTLI
    return None


@functools.cache
def brotli_module() -> types.ModuleType | None:
    """The program's own brotli, else brotlicffi, whichever first can cap what it unpacks.
    Never a dependency: httpx and httpx2 ask for br only when one of them imports."""
    try:
        module = importlib.import_module("brotli")
    except ImportError:
        module = None
    if module is not None and can_cap(module):
        return module
    try:
        module = importlib.import_module("brotlicffi")
    except ImportError:
        return None
    return module if can_cap(module) else None


def can_cap(module: types.ModuleType) -> bool:
    """Whether its decompressor stops at a size, as brotli and brotlicffi 1.2 and later do.
    An older one would unpack a small bomb whole, and brotlipy, which also installs as
    brotli, has no process at all: either is taken as not installed."""
    try:
        unpack = module.Decompressor()
        unpack.process(b"", output_buffer_limit=1)
        unpack.is_finished()
        return isinstance(module.error, type) and issubclass(module.error, Exception)
    except Exception:
        return False


def unbrotli(raw: bytes) -> bytes | None:
    """The body with br undone, cut at the cap and one byte; None if it will not unpack."""
    module = brotli_module()
    if module is None:
        return None
    unpack = module.Decompressor()
    try:
        body = bytes(unpack.process(raw, output_buffer_limit=WEB_CAP + 1))
    except module.error:  # trailing bytes after the stream are an error too
        return None
    if not unpack.is_finished() and len(body) <= WEB_CAP:
        return None  # the stream ended early: a cut-off body must not pass for a whole one
    return body[: WEB_CAP + 1]


def unpacked(raw: bytes, encoding: str) -> bytes | None:
    """The body with gzip, deflate or br undone; None if it will not unpack. Any other
    encoding comes back as it is. The one decoder for every reply the gate reads."""
    encoding = encoding.strip().lower()
    if encoding == "br":
        return unbrotli(raw)
    bits = {"gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}.get(encoding)
    if bits is None:
        return raw
    unpack = zlib.decompressobj(bits)
    try:
        # Never more than the cap and one byte: a small bomb is not unpacked whole.
        body = unpack.decompress(raw, WEB_CAP + 1)
    except zlib.error:
        return None
    if not unpack.eof and len(body) <= WEB_CAP:
        return None  # the stream ended early: a cut-off body must not pass for a whole one
    if unpack.unused_data:
        return None  # a second gzip member or trailing bytes: changing the first loses the rest
    return body


def change_web(
    status: int,
    method: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Change a web reply. A text reply is changed as text, and a JSON one as data, with find
    only. The new body goes with new headers: no encoding, the right length, and a charset that
    can hold the text. decoded: the body is already unpacked, as a reply read in memory is."""
    kind = web_kind(status, method, headers)
    if isinstance(kind, Unchangeable):
        return kind
    encoding = "" if decoded else (header(headers, "content-encoding") or "")
    plain_body = unpacked(body, encoding)
    if plain_body is None:
        return Unchangeable("the body will not unpack")
    if len(plain_body) > WEB_CAP:
        return Unchangeable("the body is over 10 MB", "data")
    declared = header(headers, "content-type") or ""
    charset = charset_of(declared)
    try:
        text = plain_body.decode(charset or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return Unchangeable("the body cannot be decoded with its charset")
    content_type = declared
    if kind == "json":
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return Unchangeable("the body is not JSON that parses")
        done = change_data(value, fault, "$")
        if isinstance(done, Unchangeable):
            return done
        try:
            written = json.dumps(
                done.value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (ValueError, RecursionError):
            return Unchangeable(
                "the JSON holds NaN or Infinity, which cannot be written back", "data"
            )
        if charset and charset.lower().replace("_", "-") not in ("utf-8", "utf8"):
            content_type = with_charset(declared, "utf-8")
        new_body = written.encode("utf-8")
        count, parts = done.count, done.parts
    else:
        said = change_text(text, fault, "body")
        if isinstance(said, Unchangeable):
            return said
        try:
            new_body = str(said.value).encode(charset or "utf-8")
        except UnicodeEncodeError:
            # Sent as UTF-8, and the type says so, as a server that changed its mind would.
            new_body = str(said.value).encode("utf-8")
            content_type = with_charset(declared, "utf-8")
        count, parts = said.count, said.parts
    kept = [(k, v) for k, v in headers if k.lower() not in (*BODY_HEADERS, "content-type")]
    kept += [("content-type", content_type), ("content-length", str(len(new_body)))]
    return WebChanged(new_body, kept, count, parts)


def streamed(word: str) -> str:
    return (
        f"{word} on a model call needs a reply that is not streamed; turn streaming off for "
        "this model, or fault it another way"
    )


# The carried faults that change a Responses reply too. replace stays on Chat Completions.
ON_RESPONSES = ("garble", "truncate", "bad tool args")


def only_on_chat(word: str, op: str) -> Unchangeable:
    return Unchangeable(
        f"{word} on a model call works on Chat Completions only, and this call is {op}; "
        "fault it another way",
        "unsupported",
    )


def chat_kind(
    status: int, op: str, headers: Sequence[tuple[str, str]], word: str = "replace"
) -> Unchangeable | None:
    """None when a model reply's headers allow a change to its content, else why not. Decided
    by the headers alone, so a stream is never read to find out."""
    if op != "chat" and not (op == "responses" and word in ON_RESPONSES):
        return only_on_chat(word, op)
    kind = media_type(header(headers, "content-type") or "")
    if kind == "text/event-stream":
        return Unchangeable(streamed(word), "unsupported")
    if status != 200:
        return Unchangeable(f"the reply status is {status}, which is not changed", "data")
    packed = packed_why(header(headers, "content-encoding") or "")
    if packed is not None:
        return Unchangeable(packed)
    if kind != "application/json":
        return Unchangeable(f"the reply is {kind or 'of no type'}, not a chat completion")
    return None


def chat_reply(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    word: str,
    *,
    decoded: bool,
) -> tuple[dict[str, object], list[object]] | Unchangeable:
    """A chat completion, parsed, and its choices; or why it cannot be changed."""
    said = chat_kind(status, op, headers, word) if op == "chat" else only_on_chat(word, op)
    if said is not None:
        return said
    encoding = "" if decoded else (header(headers, "content-encoding") or "")
    plain_body = unpacked(body, encoding)
    if plain_body is None:
        return Unchangeable("the body will not unpack")
    try:
        value = json.loads(plain_body)
    except (ValueError, RecursionError):
        return Unchangeable("the body is not JSON that parses")
    choices = cast("dict[str, object]", value).get("choices") if isinstance(value, dict) else None
    if not isinstance(choices, list):
        return Unchangeable("the reply has no choices")
    return cast("dict[str, object]", value), cast("list[object]", choices)


def message_of(choice: object) -> dict[str, object] | None:
    held = cast("dict[str, object]", choice).get("message") if isinstance(choice, dict) else None
    return cast("dict[str, object]", held) if isinstance(held, dict) else None


def rewritten(
    value: object,
    headers: Sequence[tuple[str, str]],
    count: int,
    parts: list[str],
    status: int | None = None,
) -> WebChanged:
    """The changed reply as compact JSON, with no encoding and its own length. Every other
    header stays as the provider sent it."""
    new_body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    kept = [(k, v) for k, v in headers if k.lower() not in BODY_HEADERS]
    kept.append(("content-length", str(len(new_body))))
    return WebChanged(new_body, kept, count, tuple(parts[:SHOWN_PARTS]), status)


def change_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Replace the text of each choice of a chat completion. A choice with no text, such as
    tool calls only or a refusal, keeps its place; a reply with no text anywhere is not
    changed. A model's text is changed as text, even when it looks like JSON."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts, missed = 0, list[str](), list[Unchangeable]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        text = None if message is None else message.get("content")
        if message is None or not isinstance(text, str) or not text:  # tool calls: often ""
            continue
        done = change_text(text, fault, f"choices[{i}].message.content", sniff=False)
        if isinstance(done, Unchangeable):
            missed.append(done)
            continue
        message["content"] = done.value
        count += done.count
        parts += done.parts
    if not count:
        if missed:
            return next((m for m in missed if m.mistake), missed[0])
        return Unchangeable(no_text(choices), "data")
    return rewritten(value, headers, count, parts)


# The stray token of Anthropic's postmortem of August and September 2025, its own example of
# what turned up in the middle of English replies on the Claude API.
GARBLE_TOKEN = "สวัสดี"
GARBLE_EVERY = 5
WORD = re.compile(r"\S+")


def garbled(text: str) -> tuple[str, int]:
    """Every fifth word becomes the stray token; with fewer than five, the last word does. A
    word runs between spaces or line breaks, which stay as they were. Returns the text and
    how many words changed."""
    words = list(WORD.finditer(text))
    picked = words[GARBLE_EVERY - 1 :: GARBLE_EVERY] or words[-1:]
    out: list[str] = []
    at = 0
    for word in picked:
        out += [text[at : word.start()], GARBLE_TOKEN]
        at = word.end()
    return "".join([*out, text[at:]]), len(picked)


def garble_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Put the stray token in each choice's text. Every other field stays as it was sent."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts = 0, list[str]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        text = None if message is None else message.get("content")
        if message is None or not isinstance(text, str):
            continue
        new, words = garbled(text)
        if words:
            message["content"] = new
            count += words
            parts.append(f"choices[{i}].message.content")
    if not count:
        return Unchangeable(no_text(choices), "data")
    return rewritten(value, headers, count, parts)


# OpenAI's reply, with status 400, when the token limit lands inside a tool call, saved on
# 18 November 2026: a cut tool call is never sent as one.
CUT_IN_A_TOOL_CALL: dict[str, object] = {
    "error": {
        "message": (
            "Could not finish the message because max_tokens or model output limit was "
            "reached. Please try again with higher max_tokens."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }
}


def cut(text: str) -> str:
    """The text up to the last space at or before its middle, as a token limit leaves it: the
    space starts the next token, so it goes too. With no such space, the first half, and at
    least one character."""
    middle = len(text) // 2
    at = text.rfind(" ", 1, middle + 1)
    return text[:at] if at > 0 else text[: max(1, middle)]


def has_text(message: dict[str, object]) -> bool:
    text = message.get("content")
    return isinstance(text, str) and bool(text)  # tool calls often come with ""


def truncate_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Cut each choice's text as the token limit does, with finish_reason length. A choice cut
    in its text never reached its tool calls, so they go. When any choice holds only tool
    calls, the whole reply becomes the 400 OpenAI sends when the limit lands inside one, with
    tool_choice left to the model or required, as captured. usage stays as it was billed."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    messages = [(i, choice, message_of(choice)) for i, choice in enumerate(choices)]
    if any(m is not None and not has_text(m) and m.get("tool_calls") for _, _, m in messages):
        return rewritten(CUT_IN_A_TOOL_CALL, headers, 1, ["error"], 400)
    count, parts = 0, list[str]()
    for i, choice, message in messages:
        if message is None or not has_text(message):
            continue
        message["content"] = cut(cast(str, message["content"]))
        message.pop("tool_calls", None)
        cast("dict[str, object]", choice)["finish_reason"] = "length"
        count += 1
        parts.append(f"choices[{i}].message.content")
    if count:
        return rewritten(value, headers, count, parts)
    why = no_text(choices)
    if why == "the reply has no text":
        why = "the reply has no text and no tool call"
    return Unchangeable(why, "data")


def broken_args(arguments: str) -> str | None:
    """The arguments without their last closing brace, so they no longer parse; None with no
    brace to remove."""
    at = arguments.rfind("}")
    return None if at == -1 else arguments[:at] + arguments[at + 1 :]


def break_args_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Break the arguments of every function tool call in every choice."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts = 0, list[str]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        held = None if message is None else message.get("tool_calls")
        for j, call in enumerate(cast("list[object]", held) if isinstance(held, list) else []):
            function = (
                cast("dict[str, object]", call).get("function") if isinstance(call, dict) else None
            )
            if not isinstance(function, dict):
                continue
            function = cast("dict[str, object]", function)
            arguments = function.get("arguments")
            broken = broken_args(arguments) if isinstance(arguments, str) else None
            if broken is None:
                continue
            function["arguments"] = broken
            count += 1
            parts.append(f"choices[{i}].message.tool_calls[{j}].function.arguments")
    if not count:
        return Unchangeable("the reply has no tool call with arguments to break", "data")
    return rewritten(value, headers, count, parts)


# The change each carried fault makes to a chat completion, by its word.
CHAT_CHANGES = {
    "replace": change_chat,
    "garble": garble_chat,
    "truncate": truncate_chat,
    "bad tool args": break_args_chat,
}


def response_reply(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    word: str,
    *,
    decoded: bool,
) -> tuple[dict[str, object], list[object]] | Unchangeable:
    """A Responses reply, parsed, and its output items; or why it cannot be changed."""
    said = chat_kind(status, op, headers, word)
    if said is not None:
        return said
    encoding = "" if decoded else (header(headers, "content-encoding") or "")
    plain_body = unpacked(body, encoding)
    if plain_body is None:
        return Unchangeable("the body will not unpack")
    try:
        value = json.loads(plain_body)
    except (ValueError, RecursionError):
        return Unchangeable("the body is not JSON that parses")
    output = cast("dict[str, object]", value).get("output") if isinstance(value, dict) else None
    if not isinstance(output, list):
        return Unchangeable("the reply has no output")
    return cast("dict[str, object]", value), cast("list[object]", output)


def as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def text_parts(output: list[object]) -> list[tuple[int, int, dict[str, object]]]:
    """Each output_text part with text, in order: its item, its place, and the part."""
    found: list[tuple[int, int, dict[str, object]]] = []
    for i, raw in enumerate(output):
        item = as_dict(raw)
        held = None if item is None or item.get("type") != "message" else item.get("content")
        for j, raw_part in enumerate(cast("list[object]", held) if isinstance(held, list) else []):
            part = as_dict(raw_part)
            if part is not None and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    found.append((i, j, part))
    return found


def function_calls(output: list[object]) -> list[tuple[int, dict[str, object]]]:
    """Each function_call item whose arguments are text, in order, with its place."""
    items = [(i, as_dict(raw)) for i, raw in enumerate(output)]
    return [
        (i, item)
        for i, item in items
        if item is not None
        and item.get("type") == "function_call"
        and isinstance(item.get("arguments"), str)
    ]


def no_output_text(output: list[object]) -> str:
    """Why a Responses reply had no text to change."""
    items = [as_dict(raw) or {} for raw in output]
    parts = [
        as_dict(part) or {}
        for item in items
        if item.get("type") == "message"
        for part in cast("list[object]", item.get("content") or [])
    ]
    if any(part.get("type") == "refusal" for part in parts):
        return "the reply is a refusal with no text"
    if any(item.get("type") == "function_call" for item in items):
        return "the reply has only tool calls and no text"
    return "the reply has no text"


def with_fresh_ids(value: dict[str, object], output: list[object]) -> dict[str, object]:
    """The changed reply under new ids in OpenAI's forms, for the reply and each message and
    function call. OpenAI stored the real reply under the real ids, and an item resent with a
    stored id is read as stored, not as sent: under a made-up id the model reads what the
    agent got. call_id stays, as the agent pairs its function_call_output by it."""
    value["id"] = openai_table.made_up_hex_id("resp_")
    for raw in output:
        item = as_dict(raw)
        prefix = None if item is None else openai_table.ITEM_PREFIXES.get(str(item.get("type")))
        if item is not None and prefix is not None and "id" in item:
            item["id"] = openai_table.made_up_hex_id(prefix)
    return value


def garble_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Put the stray token in each output_text part. Every other field stays as it was sent."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    count, parts = 0, list[str]()
    for i, j, part in text_parts(output):
        new, words = garbled(cast(str, part["text"]))
        part["text"] = new
        count += words
        parts.append(f"output[{i}].content[{j}].text")
    if not count:
        return Unchangeable(no_output_text(output), "data")
    return rewritten(with_fresh_ids(value, output), headers, count, parts)


def truncate_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Stop the reply as max_output_tokens does: the first text is cut, and what came after it
    was never made, so it goes. With no text, the last function call's arguments are cut. The
    item and the reply are incomplete, for max_output_tokens, as captured; usage stays as it
    was billed."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    texts, calls = text_parts(output), function_calls(output)
    if texts:
        i, j, part = texts[0]
        part["text"] = cut(cast(str, part["text"]))
        item = cast("dict[str, object]", output[i])
        item["content"] = cast("list[object]", item["content"])[: j + 1]
        where = f"output[{i}].content[{j}].text"
    elif calls:
        i, item = calls[-1]
        item["arguments"] = cut(cast(str, item["arguments"]))
        where = f"output[{i}].arguments"
    else:
        why = no_output_text(output)
        if why == "the reply has no text":
            why = "the reply has no text and no tool call"
        return Unchangeable(why, "data")
    item["status"] = "incomplete"
    del output[i + 1 :]
    value.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    if "completed_at" in value:  # an incomplete reply never completed
        value["completed_at"] = None
    return rewritten(with_fresh_ids(value, output), headers, 1, [where])


def break_args_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Break the arguments of every function_call item."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    count, parts = 0, list[str]()
    for i, item in function_calls(output):
        broken = broken_args(cast(str, item["arguments"]))
        if broken is None:
            continue
        item["arguments"] = broken
        count += 1
        parts.append(f"output[{i}].arguments")
    if not count:
        return Unchangeable("the reply has no tool call with arguments to break", "data")
    return rewritten(with_fresh_ids(value, output), headers, count, parts)


RESPONSES_CHANGES = {
    "garble": garble_responses,
    "truncate": truncate_responses,
    "bad tool args": break_args_responses,
}


def model_change(word: str, op: str) -> Callable[..., WebChanged | Unchangeable]:
    """The change a carried fault makes to a model's reply of this kind. replace on a
    Responses reply keeps the chat change, which says it works on Chat Completions only."""
    if op == "responses" and word in RESPONSES_CHANGES:
        return RESPONSES_CHANGES[word]
    return CHAT_CHANGES[word]


def no_text(choices: list[object]) -> str:
    """Why a chat completion had no text to change, from its first choice."""
    first = (
        cast("dict[str, object]", choices[0]) if choices and isinstance(choices[0], dict) else {}
    )
    message = first.get("message")
    held = cast("dict[str, object]", message) if isinstance(message, dict) else {}
    if held.get("refusal"):
        return "the reply is a refusal with no text"
    if held.get("tool_calls"):
        return "the reply has only tool calls and no text"
    return "the reply has no text"
