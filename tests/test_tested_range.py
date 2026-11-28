"""The installed libraries are inside the range each gate says it was tested with.

The lock holds the tested versions, so this passes in the normal jobs. The newest-releases
job in CI upgrades the gated libraries past the lock and runs the gate tests with this file,
so a release newer than a gate's range fails here, with the notice a team would see.
The ranges come from tests/tested_ranges.py, which finds every TESTED in the product.
"""

import importlib.metadata

import pytest

import tested_ranges
from scrimmage.state import newer_version_message

RANGES = tested_ranges.ranges()


def installed(library: str) -> tuple[int, int] | None:
    try:
        parts = importlib.metadata.version(library).split(".")
        return int(parts[0]), int(parts[1])
    except (importlib.metadata.PackageNotFoundError, ValueError, IndexError):
        return None


def test_the_scan_finds_the_ranges_the_gates_state() -> None:
    assert {"httpx", "httpx2", "langchain-core", "mcp"} <= set(RANGES)


@pytest.mark.parametrize("library", RANGES)
def test_the_installed_library_is_not_newer_than_its_tested_range(library: str) -> None:
    version = installed(library)
    assert version is not None, f"{library}: its version could not be read"
    notice = newer_version_message(library, version, RANGES[library])
    assert notice is None, f"{notice}: widen the gate's TESTED range after running its tests"
