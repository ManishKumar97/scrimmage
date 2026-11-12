"""Test support for the Scrimmage MCP server's tests: a startup hook for the processes they start.

With this folder on PYTHONPATH and SCRIMMAGE_TEST_SHIM naming a file, every Python process
runs that file as it starts: the server, each scrimmage command it starts, and each run, whose
own startup file runs this one next. The file itself decides which process it acts in.
This is test support code; it is never in a wheel.
"""

import os

_shim = os.environ.get("SCRIMMAGE_TEST_SHIM")
if _shim:
    with open(_shim, encoding="utf-8") as _file:
        exec(compile(_file.read(), _shim, "exec"), {"__name__": "scrimmage_test_shim"})
