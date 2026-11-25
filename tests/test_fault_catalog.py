"""Every fault in the catalog names where it was seen."""

import tomllib
from pathlib import Path

import pytest

from scrimmage.config import FAULTS

CATALOG = Path(__file__).parents[1] / "docs" / "fault-catalog.toml"
ORIGINS = {"public incident", "trace", "provider docs", "documented mechanism"}


def problems(catalog: dict[str, object]) -> list[str]:
    """What is wrong with a parsed catalog; empty when it is valid."""
    found: list[str] = []
    # A misspelled table such as [[faults]] would otherwise read as an empty catalog.
    found += [f"unknown table {key!r}" for key in sorted(set(catalog) - {"fault"})]
    faults = catalog.get("fault", [])
    assert isinstance(faults, list)
    names: list[object] = []
    for fault in faults:
        assert isinstance(fault, dict)
        name = fault.get("name")
        names.append(name)
        if set(fault) != {"name", "origin", "source"}:
            found.append(f"{name}: needs exactly name, origin and source")
        if fault.get("origin") not in ORIGINS:
            found.append(f"{name}: origin must be one of {sorted(ORIGINS)}")
        source = fault.get("source")
        if not isinstance(source, str) or not source.strip():
            found.append(f"{name}: source is empty")
    found += [f"{n}: listed twice" for n in sorted(set(map(str, names))) if names.count(n) > 1]
    return found


@pytest.mark.design("catalog.origin")
def test_the_catalog_is_valid() -> None:
    assert problems(tomllib.loads(CATALOG.read_text())) == []


@pytest.mark.design("catalog.origin")
@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ('origin = "guess"\nsource = "x"', "origin must be one of"),
        ('origin = "imagined"\nsource = "x"', "origin must be one of"),
        ('origin = "trace"\nsource = " "', "source is empty"),
        ('origin = "trace"', "needs exactly name, origin and source"),
    ],
)
def test_a_fault_without_a_real_origin_is_rejected(entry: str, problem: str) -> None:
    catalog = tomllib.loads(f'[[fault]]\nname = "timeout"\n{entry}\n')
    assert any(problem in p for p in problems(catalog))


@pytest.mark.design("catalog.origin")
@pytest.mark.parametrize("origin", sorted(ORIGINS))
def test_each_origin_of_the_vocabulary_is_accepted(origin: str) -> None:
    catalog = tomllib.loads(f'[[fault]]\nname = "timeout"\norigin = "{origin}"\nsource = "x"\n')
    assert problems(catalog) == []


@pytest.mark.design("catalog.origin")
def test_the_catalogs_header_lists_the_whole_vocabulary() -> None:
    header = "".join(
        line for line in CATALOG.read_text().splitlines(keepends=True) if line[0] == "#"
    )
    assert all(f'"{origin}"' in header for origin in ORIGINS), sorted(ORIGINS)


def test_a_misspelled_table_and_a_duplicate_are_rejected() -> None:
    entry = '[[fault]]\nname = "timeout"\norigin = "trace"\nsource = "x"\n'
    assert problems(tomllib.loads(entry * 2)) == ["timeout: listed twice"]
    assert problems(tomllib.loads(entry.replace("fault]", "faults]"))) == ["unknown table 'faults'"]


@pytest.mark.design("catalog.every_built_fault")
def test_every_fault_scrimmage_can_inject_is_in_the_catalog() -> None:
    names = {fault["name"] for fault in tomllib.loads(CATALOG.read_text()).get("fault", [])}
    missing = set(FAULTS) - names
    assert not missing, f"faults with no catalog entry: {sorted(missing)}"
