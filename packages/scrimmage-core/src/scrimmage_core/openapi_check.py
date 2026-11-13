"""Check a request body template against a FastAPI /openapi.json document.

A pure module: the parsed document, the route and the rendered body go in, and
warnings and notes come out. It checks a small subset of JSON Schema and nothing else.
"""

from dataclasses import dataclass
from typing import cast

REF_PREFIX = "#/components/schemas/"
REF_DEPTH = 8  # a $ref chain longer than this is a cycle, and is left unchecked
# Keywords the subset does not check. A schema that uses one earns the "not checked" note.
UNCHECKED = frozenset(
    {
        "oneOf",
        "allOf",
        "not",
        "pattern",
        "format",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
NOT_CHECKED_NOTE = (
    "/openapi.json: keywords outside the checked subset, such as oneOf, pattern and "
    "format, are not checked"
)

Schema = dict[str, object]


@dataclass(frozen=True)
class Result:
    warnings: tuple[str, ...]
    notes: tuple[str, ...]


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

    prefix is the path prefix the document was found under; it is taken off route
    before the lookup, as an app mounted under a root_path lists its paths without it.
    """
    verb = method.upper()
    where = f"[{label}] body:"
    path = _strip_prefix(route, prefix)
    request = _request_schema(document, path, method.lower())
    if request is None:
        note = f"{where} /openapi.json has no JSON request body for {verb} {path}; not checked"
        return Result((), (note,))
    schemas = _components(document)
    warnings: list[str] = []
    skipped: list[bool] = []
    says = f"{where} /openapi.json says {verb} {path}"
    _check_object(request, body, "", schemas, says, warnings, skipped, deep=True)
    return Result(tuple(warnings), (NOT_CHECKED_NOTE,) if skipped else ())


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


def _type_fits(want: object, value: object) -> bool:
    names = [want] if isinstance(want, str) else _list(want)
    if not names:
        return True  # a type this check cannot read is left alone
    have = _json_type(value)
    return any(n == have or (n == "number" and have == "integer") for n in names)


def _check_object(
    schema: Schema,
    value: object,
    name: str,
    schemas: Schema,
    says: str,
    warnings: list[str],
    skipped: list[bool],
    *,
    deep: bool,
) -> None:
    settled, nullable = _settle(schema, schemas)
    if settled is None or (value is None and nullable):
        return
    if UNCHECKED & settled.keys() or "anyOf" in settled:
        skipped.append(True)
    want = settled.get("type")
    if want is not None and not _type_fits(want, value):
        what = f'field "{name}"' if name else "the body"
        warnings.append(f"{says} wants {what} as {want}; the template has {_json_type(value)}")
        return
    members = _obj(value)
    if members is None or not deep:
        return
    given = set(members)
    for field in _list(settled.get("required")):
        if isinstance(field, str) and field not in given:
            at = f"{name}.{field}" if name else field
            warnings.append(f'{says} requires "{at}"; the template has no such field')
    props = _obj(settled.get("properties")) or {}
    if settled.get("additionalProperties") is False:
        for field in sorted(given - props.keys()):
            at = f"{name}.{field}" if name else field
            warnings.append(f'{says} allows no field "{at}"; the template has it')
    for field, sub in props.items():
        sub_schema = _obj(sub)
        if field not in given or sub_schema is None:
            continue
        at = f"{name}.{field}" if name else field
        # One level down: a property's own fields are checked, theirs are not.
        _check_object(
            sub_schema,
            members[field],
            at,
            schemas,
            says,
            warnings,
            skipped,
            deep=name == "",
        )
