"""scrimmage-run: put Scrimmage's startup folder first on PYTHONPATH, then run a command."""

import ctypes
import os
import signal
import sys
from pathlib import Path

BOOT = Path(__file__).parent / "_boot"


PR_SET_PDEATHSIG = 1  # linux/prctl.h


def stop_with_the_runner() -> None:
    """Ask Linux to send this process SIGTERM when the runner that started it dies.

    A runner killed with SIGKILL cannot stop its runs, and the time limit is only kept by the
    runner. The setting survives the exec below. Elsewhere it does nothing.
    """
    runner = os.environ.pop("SCRIMMAGE_RUNNER_PID", "")  # the program never sees it
    if not sys.platform.startswith("linux") or not runner.isdigit():
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
            return
    except (OSError, AttributeError):
        return
    if os.getppid() != int(runner):  # it died before the setting, so no signal will come
        os.kill(os.getpid(), signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: scrimmage-run command [args...]", file=sys.stderr)
        return 2
    stop_with_the_runner()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(BOOT), env.get("PYTHONPATH")) if p)
    # exec keeps this process id, so it names the program the runner started.
    env.setdefault("SCRIMMAGE_ROOT_PID", str(os.getpid()))
    try:
        os.execvpe(args[0], args, env)
    except OSError as exc:
        print(f"scrimmage-run: cannot run {args[0]}: {exc.strerror}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126


if __name__ == "__main__":
    sys.exit(main())
