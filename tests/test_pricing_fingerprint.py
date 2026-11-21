"""tools/pricing_fingerprint.py hashes the pricing tables and nothing around them."""

import html
import importlib.util
import io
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "tools" / "pricing_fingerprint.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pricing_fingerprint", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pricing_fingerprint = load()


def table(tier: str, price: float) -> str:
    return (
        f'{{"tier":[0,"{tier}"],"rows":[1,[[1,[[0,"model-a"],[0,{price}],[0,0.5]]],'
        f'[1,[[0,"model-b"],[0,2],[0,10]]]]]}}'
    )


def island(props: str) -> str:
    return f'<astro-island uid="x" props="{html.escape(props)}"><div></div></astro-island>'


def page(*tables: str, around: str = "") -> str:
    other = island('{"page":[0,{"path":[0,"/api/docs/pricing"]}]}')
    return f"<html>{around}<body>{other}{''.join(island(t) for t in tables)}</body></html>"


def run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> tuple[int, str, str]:
    """Run the script's main on stdin, as `python tools/pricing_fingerprint.py -` does."""
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = pricing_fingerprint.main(["pricing_fingerprint.py", "-"])
    out, err = capsys.readouterr()
    return code, out, err


def test_the_same_tables_give_the_same_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    html_in = page(table("standard", 1), table("batch", 0.5))
    code, out, _ = run(monkeypatch, capsys, html_in)
    assert code == 0
    assert len(out.strip()) == 64
    assert run(monkeypatch, capsys, html_in)[1] == out


def test_one_changed_number_changes_the_hash():
    before = pricing_fingerprint.fingerprint(page(table("standard", 1), table("batch", 0.5)))
    after = pricing_fingerprint.fingerprint(page(table("standard", 1.01), table("batch", 0.5)))
    assert before
    assert after
    assert before != after


def test_unrelated_html_around_the_props_does_not_change_the_hash():
    tables = (table("standard", 1), table("batch", 0.5))
    plain = pricing_fingerprint.fingerprint(page(*tables))
    banner = '<script src="/_astro/app.abc123.js?dpl=zzz"></script><p>new banner</p>'
    noisy = pricing_fingerprint.fingerprint(page(*tables, around=banner))
    assert plain is not None
    assert plain == noisy


def test_the_order_of_the_tables_on_the_page_does_not_matter():
    a, b = table("standard", 1), table("batch", 0.5)
    assert pricing_fingerprint.fingerprint(page(a, b)) == pricing_fingerprint.fingerprint(
        page(b, a)
    )


def test_a_page_with_no_pricing_props_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    code, out, err = run(monkeypatch, capsys, page())
    assert code == 1
    assert out == ""
    assert "page shape changed" in err


def test_the_script_reads_a_file_argument(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "page.html"
    path.write_text(page(table("standard", 1)))
    assert pricing_fingerprint.main(["pricing_fingerprint.py", str(path)]) == 0
    assert capsys.readouterr().out.strip() == pricing_fingerprint.fingerprint(path.read_text())


def test_the_committed_fingerprint_is_a_hash_and_a_date():
    text = (ROOT / "tools" / "pricing-fingerprint.txt").read_text()
    assert re.fullmatch(r"[0-9a-f]{64} \d{4}-\d{2}-\d{2}\n", text), text
