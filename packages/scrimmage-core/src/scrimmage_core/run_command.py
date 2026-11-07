"""scrimmage-run: put Scrimmage's startup folder first on PYTHONPATH, then run a command."""

import os
import sys
from pathlib import Path

BOOT = Path(__file__).parent / "_boot"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: scrimmage-run command [args...]", file=sys.stderr)
        return 2
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
