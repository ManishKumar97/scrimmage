"""Derail's startup file. derail-run puts this folder first on PYTHONPATH.

It does nothing unless DERAIL_TOKEN is set; then it switches the SDK on. Either
way it then runs the next sitecustomize.py on the path, which this one hid, so the
program starts exactly as it would without Derail.
"""

import importlib.machinery
import importlib.util
import os
import sys


def _chain() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module  # as a plain import of it would leave things
    spec.loader.exec_module(module)


def _is_root() -> bool:
    """Whether this is the program the test started, not a child it started.

    derail-run sets DERAIL_ROOT_PID to its own process id and then becomes the
    program, so only that program has a matching id. Unset means no derail-run,
    such as PYTHONPATH set by hand, which also starts the program under test.
    """
    root = os.environ.get("DERAIL_ROOT_PID", "")
    return not root or root == str(os.getpid())


def _start() -> None:
    try:
        from derail_sdk import bootstrap
    except ModuleNotFoundError as exc:
        if exc.name != "derail_sdk":
            raise
        if not _is_root():
            return  # a child on another Python, such as the aws CLI: leave it alone
        raise RuntimeError(
            f"derail is not installed for {sys.executable}, so the program under "
            "test would run with no gates; run it on a Python with derail installed"
        ) from None
    bootstrap.start()


if os.environ.get("DERAIL_TOKEN"):
    try:
        _start()
    except BaseException as exc:  # fail closed: never run the agent without its gates
        sys.stderr.write(f"derail: could not switch on ({exc!r}); the program stops\n")
        sys.stderr.flush()
        os._exit(1)
_chain()
