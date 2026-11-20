"""The installed libraries are inside the range each gate says it was tested with.

The lock holds the tested versions, so this passes in the normal jobs. The newest-releases
job in CI upgrades the gated libraries past the lock and runs the gate tests with this file,
so a release newer than a gate's range fails here, with the notice a team would see.
"""

import importlib

import pytest

from scrimmage_core import http_gate, langchain_gate, mcp_gate
from scrimmage_core.state import newer_version_message


def found() -> list[tuple[str, tuple[int, int] | None, tuple[tuple[int, int], tuple[int, int]]]]:
    http = [
        (name, http_gate.version(importlib.import_module(name)), tested)
        for name, tested in http_gate.TESTED.items()
    ]
    return [
        *http,
        (langchain_gate.LIBRARY, langchain_gate.version(), langchain_gate.TESTED),
        ("mcp", mcp_gate.version(), mcp_gate.TESTED),
    ]


@pytest.mark.parametrize("library", ["httpx", "httpx2", "langchain-core", "mcp"])
def test_the_installed_library_is_not_newer_than_its_tested_range(library: str) -> None:
    (name, version, tested) = next(row for row in found() if row[0] == library)
    assert version is not None, f"{name}: its version could not be read"
    notice = newer_version_message(name, version, tested)
    assert notice is None, f"{notice}: widen the gate's TESTED range after running its tests"
