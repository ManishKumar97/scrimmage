"""The report's text, byte for byte, as main printed it before the report was split."""

import io

import pytest

from report_cases import CASES, FOLDER, GOLDEN
from scrimmage_core.runner import report


@pytest.mark.design("json.text_unchanged")
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_text_is_byte_for_byte_what_it_was_before_the_split(name: str) -> None:
    case = CASES[name]()
    out = io.StringIO()
    code = report(case.config, case.results, FOLDER, out, case.runner)  # type: ignore[arg-type]
    assert f"exit {code}\n" + out.getvalue() == (GOLDEN / f"{name}.txt").read_text()


def test_every_golden_file_has_its_case() -> None:
    assert {p.stem for p in GOLDEN.glob("*.txt")} == set(CASES)
