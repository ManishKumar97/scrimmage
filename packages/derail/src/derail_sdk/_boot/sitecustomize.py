"""Derail's startup file. derail-run puts this folder first on PYTHONPATH.

It does nothing unless DERAIL_TOKEN is set; then it switches the SDK on. Either
way it then runs the next sitecustomize.py on the path, which this one hid, so the
program starts exactly as it would without Derail.

Every Python program the agent starts runs this file too, so it uses only the standard
library, and on a Python that is not the run's it prints nothing.
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


def _python() -> str:
    """This Python: its binary and its environment.

    In one venv python and python3 are the same Python. Two venvs made from one binary
    are not, because each has its own packages.
    """
    return os.path.realpath(sys.executable) + os.pathsep + sys.prefix


def _is_root() -> bool:
    """Whether this is the program the test started, not a child it started.

    The first Python in a run claims it by setting DERAIL_PYTHON. It can have any process
    id, as uv run starts Python as a child of its own. DERAIL_ROOT_PID is the id derail-run
    gave the program, which it keeps through exec. Neither set means no derail-run, such as
    PYTHONPATH set by hand, which also starts the program under test.
    """
    claimed = "DERAIL_PYTHON" in os.environ
    return not claimed or os.environ.get("DERAIL_ROOT_PID") == str(os.getpid())


class _KeepOut:
    """Stops a listed tool's module from loading on a Python that cannot gate it.

    The tool's write must not run with no gate, and no other program here is touched.
    """

    def __init__(self, modules: set[str]) -> None:
        self.modules = modules

    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        if name in self.modules:
            message = (
                f"{name} holds a tool under [agent] tools, and {sys.executable} is not the "
                "run's Python, so Derail cannot gate it"
            )
            _tell_the_runner(message)
            raise ImportError(f"derail: {message}")


def _tell_the_runner(message: str) -> None:
    """This Python may not have the SDK, so a line beside the answer reaches the runner."""
    import json

    process = {"python": sys.executable, "command": " ".join(sys.argv), "parent": os.getppid()}
    line = json.dumps({"pid": os.getpid(), "message": message, "process": process})
    try:
        with open(os.environ["DERAIL_ANSWER_FILE"] + ".refused", "a") as out:
            out.write(line + "\n")
    except (KeyError, OSError):
        pass  # the import still fails, so the write still does not run


def _keep_out_tools() -> None:
    entries = os.environ.get("DERAIL_TOOLS", "").split(",")
    modules = {entry.partition(":")[0] for entry in entries if entry}
    if modules:
        sys.meta_path.insert(0, _KeepOut(modules))


def _start() -> None:
    root = _is_root()
    same = os.environ.get("DERAIL_PYTHON") == _python()
    if not (root or same or os.environ.get("DERAIL_CHILDREN")):
        _keep_out_tools()  # another Python, such as the aws CLI: leave it alone
        return
    try:
        from derail_sdk import bootstrap
    except ModuleNotFoundError as exc:
        if exc.name != "derail_sdk":
            raise
        if not root:
            _keep_out_tools()
            return
        raise RuntimeError(
            f"derail is not installed for {sys.executable}, so the program under "
            "test would run with no gates; run it on a Python with derail installed"
        ) from None
    if root:
        os.environ["DERAIL_PYTHON"] = _python()
    bootstrap.start(child=not root)


if os.environ.get("DERAIL_TOKEN"):
    try:
        _start()
    except BaseException as exc:  # fail closed: never run the agent without its gates
        sys.stderr.write(f"derail: could not switch on ({exc!r}); the program stops\n")
        sys.stderr.flush()
        os._exit(1)
_chain()
