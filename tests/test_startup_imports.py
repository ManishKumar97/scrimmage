"""Importing the command line must stay cheap: nothing compiled, no unused module loaded."""

import json
import subprocess
import sys

import pytest

PROBE = """
import functools, json, sys
import scrimmage_core.cli
from scrimmage_core import redact

getters = {
    name: value.cache_info().currsize
    for name, value in vars(redact).items()
    if hasattr(value, "cache_info") and isinstance(value, functools._lru_cache_wrapper)
}
print(json.dumps(["secrets" in sys.modules, getters]))
"""


@pytest.mark.slow  # it starts a Python
def test_importing_the_cli_loads_no_secrets_and_compiles_no_redact_pattern() -> None:
    done = subprocess.run([sys.executable, "-c", PROBE], check=True, capture_output=True, text=True)
    secrets_loaded, getters = json.loads(done.stdout)
    assert not secrets_loaded, "secrets was imported at import time of scrimmage_core.cli"
    assert getters, "no redact getter found; the probe looks at nothing"
    compiled = {name: size for name, size in getters.items() if size}
    assert not compiled, f"redact patterns compiled at import: {compiled}"
