"""An MkDocs hook: builds the Schemas page from the JSON Schema files that ship in the package.

The page holds a marker line. The hook replaces it with one section per shipped schema,
in the order below, then any other schema by name. Each section has a table per object and
the full file. The files are also published next to the page, so an editor can point at
them. Nothing on the page is copied by hand, so it cannot drift from the package.

It imports nothing from mkdocs at the top, so the tests can load it without the docs group.
"""

import html
import json
import re
from pathlib import Path
from typing import Any

SCHEMAS = Path(__file__).parents[2] / "packages" / "scrimmage" / "src" / "scrimmage_core" / "schemas"
PAGE = "schemas.md"
MARKER = "<!-- shipped schemas -->"
FIRST = ["scrimmage-toml.v1.json", "cli.v1.json", "record.v1.json", "plan.v1.json"]
PUBLISHED = "schemas"

Schema = dict[str, Any]


def shipped(folder: Path = SCHEMAS) -> list[Path]:
    """Every schema file in the package, the known ones first."""
    files = {p.name: p for p in folder.glob("*.json")}
    known = [files.pop(name) for name in FIRST if name in files]
    return known + [files[name] for name in sorted(files)]


def code(text: object) -> str:
    return f"<code>{html.escape(str(text))}</code>"


def prose(text: str) -> str:
    """Plain text with its `code` spans kept as code."""
    parts = re.split(r"`([^`]+)`", text)
    return "".join(code(p) if i % 2 else html.escape(p) for i, p in enumerate(parts))


class Renderer:
    def __init__(self, name: str, schema: Schema) -> None:
        self.name = name
        self.schema = schema
        self.defs: dict[str, Schema] = schema.get("$defs", {})

    def anchor(self, definition: str) -> str:
        stem = self.name.removesuffix(".json").replace(".", "-")
        return f"{stem}-{definition}".lower()

    def resolve(self, ref: str) -> tuple[str, Schema]:
        name = ref.removeprefix("#/$defs/")
        return name, self.defs[name]

    def fields(self, schema: Schema) -> tuple[dict[str, Any], set[str]]:
        """The keys of a table and the required ones, its own and those of what it extends."""
        properties: dict[str, Any] = {}
        required: set[str] = set()
        for part in schema.get("allOf", []):
            if isinstance(part, dict) and "$ref" in part:
                more, needed = self.fields(self.resolve(part["$ref"])[1])
                properties.update(more)
                required |= needed
        properties.update(schema.get("properties", {}))
        return properties, required | set(schema.get("required", []))

    def is_table(self, schema: Schema) -> bool:
        return bool(self.fields(schema)[0])

    def kind(self, schema: Schema | bool) -> str:
        """A short description of what a value must be."""
        if isinstance(schema, bool):
            return "any value" if schema else "not allowed"
        if schema.get("not") == {}:
            return "not allowed"
        if not set(schema) - {"description", "title"}:
            return "any value"
        if "$ref" in schema:
            name, target = self.resolve(schema["$ref"])
            if self.is_table(target):
                return f'<a href="#{self.anchor(name)}">{html.escape(name)}</a>'
            return self.kind(target) + self.limits(schema)
        if "const" in schema:
            return code(json.dumps(schema["const"]))
        if "enum" in schema:
            return "one of " + ", ".join(code(json.dumps(v)) for v in schema["enum"])
        for key in ("anyOf", "oneOf"):
            if key in schema and all(self.kind(s) for s in schema[key]):
                return " or ".join(self.kind(s) for s in schema[key])
        if self.is_table(schema):
            return "table"
        if "allOf" in schema and len(schema["allOf"]) == 1:
            return self.kind(schema["allOf"][0])
        kind = schema.get("type")
        names = {
            "string": "text",
            "integer": "whole number",
            "number": "number",
            "boolean": "true or false",
            "null": "null",
        }
        if isinstance(kind, list):
            return " or ".join(names.get(str(k), str(k)) for k in kind) + self.limits(schema)
        if kind == "array":
            items = schema.get("items")
            inner = self.kind(items) if isinstance(items, dict) else ""
            return (f"list of {inner}" if inner else "list") + self.limits(schema)
        if kind == "object":
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and self.kind(extra):
                return f"table of {self.kind(extra)}"
            return "table"
        if isinstance(kind, str):
            return names.get(kind, kind) + self.limits(schema)
        return ""

    def limits(self, schema: Schema) -> str:
        words = {
            "pattern": "matching {}",
            "minimum": "at least {}",
            "exclusiveMinimum": "above {}",
            "maximum": "at most {}",
            "exclusiveMaximum": "below {}",
            "minLength": "{} or more characters",
            "maxLength": "{} or fewer characters",
            "minItems": "{} or more items",
            "maxItems": "{} or fewer items",
        }
        found = [words[k].format(code(schema[k])) for k in words if k in schema]
        return f", {', '.join(found)}" if found else ""

    def table(self, title: str, anchor: str, schema: Schema) -> str:
        properties, required = self.fields(schema)
        rows = []
        for key, value in properties.items():
            text = value.get("description", "") if isinstance(value, dict) else ""
            if isinstance(value, dict) and "$ref" in value and not text:
                text = self.resolve(value["$ref"])[1].get("description", "")
            rows.append(
                "<tr>"
                f"<td>{code(key)}</td>"
                f"<td>{self.kind(value)}</td>"
                f"<td>{'yes' if key in required else ''}</td>"
                f"<td>{prose(text)}</td>"
                "</tr>"
            )
        closed = schema.get("additionalProperties") is False
        about = schema.get("description", "")
        lines = [f"### `{title}` {{#{anchor}}}", ""]
        if about:
            lines += [f"<p>{prose(about)}</p>", ""]
        if closed:
            lines += ["<p>Any other key is an error.</p>", ""]
        lines.append(
            "<table><thead><tr><th>Key</th><th>Value</th><th>Required</th><th>About</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
        return "\n".join(lines) + "\n"

    def section(self) -> str:
        schema = self.schema
        title = schema.get("title", self.name)
        parts = [
            f"## {title}",
            "",
            prose(schema.get("description", "")),
            "",
            f"File: [`{self.name}`]({PUBLISHED}/{self.name}), id `{schema.get('$id', '')}`.",
            "",
        ]
        tables = []
        if self.is_table(schema):
            tables.append(self.table("top level", self.anchor("top"), schema))
        for name, definition in self.defs.items():
            if self.is_table(definition):
                tables.append(self.table(name, self.anchor(name), definition))
        if tables:
            parts += tables
        body = json.dumps(schema, indent=2)
        parts += [f'??? note "The whole of {self.name}"', "", "    ```json"]
        parts += [f"    {line}" for line in body.splitlines()]
        parts += ["    ```", ""]
        return "\n".join(parts)


def render(files: list[Path]) -> str:
    return "\n".join(Renderer(p.name, json.loads(p.read_text())).section() for p in files)


def on_page_markdown(markdown: str, page: Any, **_: Any) -> str:
    if page.file.src_uri != PAGE:
        return markdown
    if MARKER not in markdown:
        raise ValueError(f"{PAGE} must hold the line {MARKER}")
    return markdown.replace(MARKER, render(shipped()))


def on_files(files: Any, config: Any) -> Any:
    from mkdocs.structure.files import File  # only under mkdocs

    for path in shipped():
        files.append(File.generated(config, f"{PUBLISHED}/{path.name}", content=path.read_bytes()))
    return files
