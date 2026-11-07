"""The Schemas page is built from every schema that ships, and says what each key holds."""

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]


def hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("schemas_hook", ROOT / "docs/hooks/schemas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.design("docs.schemas_generated")
def test_every_shipped_schema_is_on_the_page_with_every_table() -> None:
    schemas = hook()
    files = schemas.shipped()
    shipped = ROOT / "packages/scrimmage-core/src/scrimmage_core/schemas"
    assert {p.name for p in files} == {p.name for p in shipped.glob("*.json")}
    page = schemas.render(files)
    for path in files:
        schema = json.loads(path.read_text())
        renderer = schemas.Renderer(path.name, schema)
        assert f"## {schema['title']}" in page
        assert f"({schemas.PUBLISHED}/{path.name})" in page
        for name, definition in schema.get("$defs", {}).items():
            if renderer.is_table(definition):
                assert f"{{#{renderer.anchor(name)}}}" in page, name
    assert schemas.MARKER in (ROOT / "docs" / schemas.PAGE).read_text()


def test_no_key_on_the_page_has_an_empty_value_column() -> None:
    page = hook().render(hook().shipped())
    rows = re.findall(r"<tr><td><code>([^<]+)</code></td><td>(.*?)</td>", page)
    assert rows
    assert [key for key, kind in rows if not kind] == []
