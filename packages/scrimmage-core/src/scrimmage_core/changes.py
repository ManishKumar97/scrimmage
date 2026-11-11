"""The change an append or replace fault makes to a result, as pure functions.

No I/O and no state. A text takes append and replace. Structured data (a dict, a list, a
tuple, or text that is a JSON object or array) takes only replace with find, on string
values, never on keys. A value that cannot take the change comes back unchanged with the
reason, so the gate can say why on the line. Redaction comes first: a change that lands on
text the record would remove is not made, because the line could then not show what the
agent saw.
"""

import copy
import json
import types
import typing
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from scrimmage_core.config import Fault
from scrimmage_core.redact import redact, spans

MAX_DEPTH = 100  # nesting deeper than this is not changed, so no value can overflow the stack
SHOWN_PARTS = 5
PART_CUT = 80

Kind = Literal["shape", "data", "declared", "span", "schema"]


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
    """

    why: str
    kind: Kind = "shape"
    rule: str | None = None

    @property
    def mistake(self) -> bool:
        """Whether this is a mistake in the test at once, not a call left out of the row."""
        return self.kind in ("declared", "span", "schema")

    def message(self, fault: Fault, label: str) -> str | None:
        """The mistake as the team reads it, naming the fix. None when it is no mistake."""
        if self.kind == "span":
            return (
                f"{fault.do} on {label} would change text the {self.rule} rule removes from the "
                "record, so the line could not show what changed; choose a find away from it"
            )
        if self.kind == "declared":
            return f"{label} {self.why}"
        return self.why if self.kind == "schema" else None


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


def declared_return(function: object) -> object:
    """The return annotation of a function, resolved; None when there is none or it cannot be."""
    try:
        hints = typing.get_type_hints(function)
    except Exception:  # a forward reference that cannot be resolved, or no hints at all
        return None
    return hints.get("return", None) if "return" in hints else None


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
PACKED = ("", "identity", "gzip", "deflate")  # content-encodings a body is read through
BODY_HEADERS = ("content-length", "content-encoding", "transfer-encoding")


@dataclass(frozen=True)
class WebChanged:
    """A changed reply: the body, the headers it goes with, and what changed."""

    body: bytes
    headers: list[tuple[str, str]]
    count: int
    parts: tuple[str, ...]


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
    encoding = (header(headers, "content-encoding") or "").strip().lower()
    if encoding not in PACKED:
        return Unchangeable(f"the reply is encoded with {encoding}, which is not changed")
    declared = header(headers, "content-type")
    if declared is None:
        return Unchangeable("the reply has no content-type, so its body is not read as text")
    kind = media_type(declared)
    if kind == "application/json" or (kind.startswith("application/") and kind.endswith("+json")):
        return "json"
    if kind.startswith("text/") and kind != "text/event-stream":
        return "text"
    return Unchangeable(f"the reply is {kind}, which is not text or JSON")


def unpacked(raw: bytes, encoding: str) -> bytes | None:
    """The body with gzip or deflate undone; None if it will not unpack."""
    encoding = encoding.strip().lower()
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
