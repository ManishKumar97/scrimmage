"""Check a request body template against a FastAPI /openapi.json document.

A pure module: the parsed document, the route and the rendered body go in, and
warnings and notes come out. It checks a small subset of JSON Schema and nothing else.
"""

from dataclasses import dataclass, field
from typing import cast

REF_PREFIX = "#/components/schemas/"
REF_DEPTH = 8  # a $ref chain longer than this is a cycle, and is left unchecked
# Keywords the code reads, and annotations that change no check. Any other keyword in a
# schema that is checked earns the "not checked" note.
READ = frozenset({"type", "required", "properties", "additionalProperties", "$ref", "anyOf"})
ANNOTATIONS = frozenset(
    {"title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly"}
)
NOT_CHECKED_NOTE = (
    "/openapi.json: some of the schema is not checked, such as items, oneOf, pattern and "
    "format, a $ref outside components/schemas, and fields more than one level down"
)

Schema = dict[str, object]


@dataclass(frozen=True)
class Result:
    warnings: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass
class _Out:
    says: str
    where: str
    warnings: list[str] = field(default_factory=lambda: [])
    notes: list[str] = field(default_factory=lambda: [])
    skipped: bool = False


def check_template(
    document: object,
    route: str,
    method: str,
    body: object,
    *,
    label: str = "agent",
    prefix: str = "",
) -> Result:
    """Check body against what document says POST route takes.

    prefix is the path prefix the document was found under. The route with it taken
    off is looked up first, as an app mounted under a root_path lists its paths without
    it; then the route as given, as an APIRouter(prefix=...) lists it.
    """
    verb = method.upper()
    where = f"[{label}] body:"
    path = _strip_prefix(route, prefix)
    request = _request_schema(document, path, method.lower())
    if request is None and path != route:
        path = route
        request = _request_schema(document, path, method.lower())
    if request is None:
        note = f"{where} /openapi.json has no JSON request body for {verb} {route}; not checked"
        return Result((), (note,))
    out = _Out(says=f"{where} /openapi.json says {verb} {path}", where=where)
    _check_object(request, body, "", _components(document), out, deep=True)
    notes = list(out.notes) + ([NOT_CHECKED_NOTE] if out.skipped else [])
    return Result(tuple(out.warnings), tuple(notes))


def _strip_prefix(route: str, prefix: str) -> str:
    prefix = prefix.rstrip("/")
    if prefix and (route == prefix or route.startswith(prefix + "/")):
        return route[len(prefix) :] or "/"
    return route


def _obj(value: object) -> Schema | None:
    if isinstance(value, dict):
        return {str(k): v for k, v in cast("dict[object, object]", value).items()}
    return None


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _dig(value: object, *keys: str) -> Schema | None:
    found = _obj(value)
    for key in keys:
        if found is None:
            return None
        found = _obj(found.get(key))
    return found


def _components(document: object) -> Schema:
    return _dig(document, "components", "schemas") or {}


def _request_schema(document: object, path: str, method: str) -> Schema | None:
    media = _dig(document, "paths", path, method, "requestBody", "content", "application/json")
    return None if media is None else _obj(media.get("schema")) or {}


def _resolve(schema: Schema, schemas: Schema) -> Schema | None:
    """Follow $ref inside components/schemas only. None: a $ref that is not followed."""
    for _ in range(REF_DEPTH):
        ref = schema.get("$ref")
        if ref is None:
            return schema
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            return None
        target = _obj(schemas.get(ref[len(REF_PREFIX) :]))
        if target is None:
            return None
        schema = target
    return None


def _nullable(schema: Schema) -> tuple[Schema, bool]:
    """pydantic v2's optional field, anyOf [X, null]: X, and whether null is allowed."""
    parsed = [_obj(b) for b in _list(schema.get("anyOf"))]
    plain = [b for b in parsed if b is not None and b.get("type") != "null"]
    if len(plain) == 1 and len(parsed) == 2 and all(b is not None for b in parsed):
        return plain[0], True
    return schema, False


def _settle(schema: Schema, schemas: Schema) -> tuple[Schema | None, bool]:
    """The schema to check a value against, after a $ref and a nullable anyOf."""
    nullable = False
    for _ in range(REF_DEPTH):
        resolved = _resolve(schema, schemas)
        if resolved is None:
            return None, nullable
        inner, null_here = _nullable(resolved)
        nullable = nullable or null_here
        if inner is resolved:
            return resolved, nullable
        schema = inner
    return None, nullable


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _names(want: object) -> list[str]:
    return [n for n in [want] if isinstance(n, str)] or [
        n for n in _list(want) if isinstance(n, str)
    ]


def _type_fits(names: list[str], value: object) -> bool:
    if not names:
        return True  # a type this check cannot read is left alone
    have = _json_type(value)
    integral = isinstance(value, float) and value.is_integer()
    for n in names:
        if n == have or (n == "number" and have == "integer") or (n == "integer" and integral):
            return True
    return False


def _coerced(names: list[str], value: object) -> bool:
    """A string that pydantic's lax mode turns into the wanted type."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if "integer" in names and text.lstrip("+-").isdigit():
        return True
    if "number" in names:
        try:
            float(text)
        except ValueError:
            pass
        else:
            return True
    return "boolean" in names and text.lower() in ("true", "false")


def _check_object(
    schema: Schema,
    value: object,
    name: str,
    schemas: Schema,
    out: _Out,
    *,
    deep: bool,
) -> None:
    settled, nullable = _settle(schema, schemas)
    if settled is None:
        out.skipped = True  # a $ref not followed, or a chain cut at REF_DEPTH
        return
    if value is None and nullable:
        return
    others = settled.keys() - READ - ANNOTATIONS
    extra = settled.get("additionalProperties")
    if others or "anyOf" in settled or not isinstance(extra, bool | None):
        out.skipped = True
    names = _names(settled.get("type"))
    if not _type_fits(names, value):
        what = f'field "{name}"' if name else "the body"
        if name and _coerced(names, value):
            out.notes.append(
                f"{out.where} {what} is a string where /openapi.json wants "
                f"{' or '.join(names)}; FastAPI usually coerces this (heuristic)"
            )
            return
        out.warnings.append(
            f"{out.says} wants {what} as {' or '.join(names)}; the template has {_json_type(value)}"
        )
        return
    members = _obj(value)
    if members is None:
        return
    if not deep:
        if settled.keys() & {"properties", "required"} or extra is False:
            out.skipped = True  # fields two levels down are not checked
        return
    given = set(members)
    for req in _list(settled.get("required")):
        if isinstance(req, str) and req not in given:
            at = f"{name}.{req}" if name else req
            out.warnings.append(f'{out.says} requires "{at}"; the template has no such field')
    props = _obj(settled.get("properties")) or {}
    if extra is False:
        for key in sorted(given - props.keys()):
            at = f"{name}.{key}" if name else key
            out.warnings.append(f'{out.says} allows no field "{at}"; the template has it')
    for key, sub in props.items():
        sub_schema = _obj(sub)
        if key not in given or sub_schema is None:
            continue
        at = f"{name}.{key}" if name else key
        # One level down: a property's own fields are checked, theirs are not.
        _check_object(sub_schema, members[key], at, schemas, out, deep=name == "")
