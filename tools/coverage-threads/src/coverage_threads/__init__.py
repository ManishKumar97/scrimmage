"""A coverage.py plugin, for this repository's tests only: coverage must not delete a module
another thread is importing.

When sys.path has changed, coverage's should_trace, on whichever thread first meets a new
file, calls InOrOut.set_matchers_depending_on_syspath. That looks up each source_pkgs entry
inside sys_modules_saved(), which then deletes every module added to sys.modules meanwhile,
by any thread. A module another thread is importing at that moment is deleted from under
it, and its import fails with KeyError in importlib._bootstrap._load_unlocked. In an agent
process the SDK's sender thread meets a new file just after the interpreter puts sys.path[0]
in place, while the main thread imports the agent, so runs crashed at random.

Our source_pkgs are top-level names, and finding a top-level name's spec imports nothing,
so the save-and-delete has nothing of its own to undo, and this plugin makes it do nothing.
A path in `source` does not help: the save-and-delete still runs, around an empty loop,
and the race stays. Drop this plugin once coverage.py stops deleting other threads' modules.
"""

import contextlib
from typing import Any

import coverage.inorout


def coverage_init(reg: Any, options: Any) -> None:
    """coverage.py calls this as it starts, in each process it measures."""
    del reg, options
    if not hasattr(coverage.inorout, "sys_modules_saved"):
        raise RuntimeError(
            "coverage.inorout.sys_modules_saved is gone: check whether coverage.py still "
            "deletes other threads' imports, and drop the coverage_threads plugin if not"
        )
    coverage.inorout.sys_modules_saved = contextlib.nullcontext  # type: ignore[assignment]
