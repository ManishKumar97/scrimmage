"""Scrimmage's startup file. scrimmage-run puts this folder first on PYTHONPATH.

It does nothing unless SCRIMMAGE_TOKEN, or a service's SCRIMMAGE_TOKEN_FILE, is set; then
it switches the SDK on. With SCRIMMAGE_SERVICE=1 every Python with the SDK is a service
process, which takes its runs from requests. Either
way it then runs the next sitecustomize.py on the path, which this one hid, so the
program starts exactly as it would without Scrimmage.

Every Python program the agent starts runs this file too, on whatever version that is, so
it uses only the standard library and syntax Python 3.8 can run, and on a Python that is
not the run's it prints nothing.
"""

import importlib.machinery
import importlib.util
import os
import sys
import time

CLAIM_WAIT_S = 5.0


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
    """This Python: its environment and its version.

    In one venv python and python3 are the same Python, even when --copies makes them two
    files. Two venvs made from one binary are not, because each has its own packages.
    """
    return "{}{}{}.{}".format(sys.prefix, os.pathsep, *sys.version_info[:2])


def _is_program() -> bool:
    """Whether this process is the one scrimmage-run became, or one started by hand.

    It is the program under test for certain. Through a wrapper it can only be the first
    Python that claims the run.
    """
    mine = str(os.getpid())
    if "SCRIMMAGE_PYTHON" in os.environ:  # a root that execs itself keeps scrimmage-run's id
        return os.environ.get("SCRIMMAGE_ROOT_PID") == mine
    return os.environ.get("SCRIMMAGE_ROOT_PID", mine) == mine


def _claim() -> "str | None":
    """Claim the run for this Python. None if it is the first, else the Python that was.

    Each process has its own environment, so two siblings under a shell or uv cannot tell
    each other anything there. A file beside the answer is shared, and O_EXCL lets one win.
    """
    answer = os.environ.get("SCRIMMAGE_ANSWER_FILE")
    if not answer:
        return None  # no run folder, as with PYTHONPATH set by hand
    claim = answer + ".root"
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as out:
            out.write(_python())
        return None
    deadline = time.monotonic() + CLAIM_WAIT_S
    while time.monotonic() < deadline:  # the winner writes just after it creates the file
        with open(claim) as claimed:
            winner = claimed.read()
        if winner:
            return winner
        time.sleep(0.001)
    raise RuntimeError(f"the first Python of this run never finished its claim on {claim}")


class _KeepOut:
    """Stops a listed tool's module from loading on a Python that cannot gate it.

    The tool's write must not run with no gate, and no other program here is touched.
    """

    def __init__(self, modules: "set[str]") -> None:
        self.modules = modules

    def _means_a_tool(self, name: str, path: object) -> bool:
        """A listed module by its own name, or its file by a shorter one."""
        if name in self.modules:
            return True
        last = name.rpartition(".")[2]
        close = [m for m in self.modules if m.rpartition(".")[2] == last]
        finder = importlib.machinery.PathFinder
        spec = finder.find_spec(name, path) if close else None  # type: ignore[arg-type]
        origin = str(getattr(spec, "origin", ""))
        return any(origin.endswith(os.sep + m.replace(".", os.sep) + ".py") for m in close)

    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        if self._means_a_tool(name, path):
            message = (
                f"{name} holds a tool under [agent] tools, and {sys.executable} is not the "
                "run's Python, so Scrimmage cannot gate it"
            )
            _tell_the_runner(message)
            raise ImportError(f"scrimmage: {message}")


def _tell_the_runner(message: str) -> None:
    """This Python may not have the SDK, so a line beside the answer reaches the runner."""
    import json

    process = {"python": sys.executable, "command": " ".join(sys.argv), "parent": os.getppid()}
    line = json.dumps({"pid": os.getpid(), "message": message, "process": process})
    try:
        with open(os.environ["SCRIMMAGE_ANSWER_FILE"] + ".refused", "a") as out:
            out.write(line + "\n")
    except (KeyError, OSError):
        pass  # the import still fails, so the write still does not run


def _target() -> "str | None":
    """The module of python -m, which the arguments here show only as -m.

    Python reads flags as bundles, so -um x and -mx are this too.
    """
    args = iter(getattr(sys, "orig_argv", [])[1:])
    for arg in args:
        if arg == "--check-hash-based-pycs":  # its value is the next word
            next(args, None)
            continue
        if not arg.startswith("-") or arg in ("-", "--"):
            return None
        for place, letter in enumerate("" if arg.startswith("--") else arg[1:], 2):
            if letter == "c":
                return None
            if letter == "m":
                return arg[place:] or next(args, None)
            if letter in "WX":
                if place == len(arg):
                    next(args, None)
                break
    return None


def _keep_out_tools() -> None:
    entries = os.environ.get("SCRIMMAGE_TOOLS", "").split(",")
    modules = {entry.partition(":")[0] for entry in entries if entry}
    if not modules:
        return
    sys.meta_path.insert(0, _KeepOut(modules))
    # The program itself is never imported, so the finder cannot stop it.
    script = os.path.abspath(sys.argv[0]).rpartition(".")[0].replace(os.sep, ".")
    target = _target()
    for module in modules:
        if target in (module, module + ".__main__") or script.endswith("." + module):
            message = (
                f"{module} holds a tool under [agent] tools, and {sys.executable} is not the "
                "run's Python, so Scrimmage cannot gate it"
            )
            _tell_the_runner(message)
            raise RuntimeError(message)


def _start() -> None:
    has_sdk = importlib.util.find_spec("scrimmage_core") is not None
    theirs = os.environ.get("SCRIMMAGE_PYTHON")
    program = _is_program()
    if not has_sdk and program:
        raise RuntimeError(
            f"scrimmage-core is not installed for {sys.executable}, so the program under "
            "test would run with no gates; run it on a Python with scrimmage-core installed"
        )
    if not has_sdk:
        _keep_out_tools()  # a tool, a build step or a cloud command: leave it alone
        return
    if os.environ.get("SCRIMMAGE_SERVICE") == "1":
        # Every Python of a service is a service process: the supervisor, each worker, a
        # reloader. None is a child, and none has a run of its own.
        from scrimmage_core import bootstrap

        bootstrap.start_service()
        return
    root = program and theirs is not None  # it execs itself and stays the root
    if theirs is None:
        theirs = _claim()
        root = theirs is None
    joins = root or theirs == _python() or os.environ.get("SCRIMMAGE_CHILDREN")
    if not joins:
        os.environ["SCRIMMAGE_PYTHON"] = str(theirs)
        _keep_out_tools()  # another Python, such as the aws CLI: leave it alone
        return
    os.environ["SCRIMMAGE_PYTHON"] = str(theirs or _python())

    from scrimmage_core import bootstrap

    bootstrap.start(mode="program" if root else "child", target=_target())


if os.environ.get("SCRIMMAGE_TOKEN") or os.environ.get("SCRIMMAGE_TOKEN_FILE"):
    try:
        _start()
    except BaseException as exc:  # fail closed: never run the agent without its gates
        sys.stderr.write(f"scrimmage: could not switch on ({exc!r}); the program stops\n")
        sys.stderr.flush()
        os._exit(1)
_chain()
