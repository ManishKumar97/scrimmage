"""Every fault in the catalog names where it was seen."""

import tomllib
from pathlib import Path

import pytest

CATALOG = Path(__file__).parents[1] / "docs" / "fault-catalog.toml"
ORIGINS = {"public incident", "trace"}


def load() -> list[dict[str, object]]:
    catalog = tomllib.loads(CATALOG.read_text())
    # A misspelled table such as [[faults]] would otherwise read as an empty catalog.
    assert set(catalog) <= {"fault"}, (
        f"unknown tables: {sorted(set(catalog) - {'fault'})}"
    )
    return catalog.get("fault", [])


@pytest.mark.design("catalog.origin")
def test_every_fault_names_its_origin() -> None:
    for fault in load():
        assert set(fault) == {"name", "origin", "source"}, fault
        assert fault["origin"] in ORIGINS, fault
        source = fault["source"]
        assert isinstance(source, str) and source.strip(), fault


def test_fault_names_are_unique() -> None:
    names = [fault["name"] for fault in load()]
    assert len(names) == len(set(names))
