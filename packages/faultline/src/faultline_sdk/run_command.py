"""faultline-run: put Faultline's startup folder first on PYTHONPATH, then run a command."""

import os
import sys
from pathlib import Path

BOOT = Path(__file__).parent / "_boot"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: faultline-run command [args...]", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(BOOT), env.get("PYTHONPATH")) if p)
    os.execvpe(args[0], args, env)


if __name__ == "__main__":
    sys.exit(main())
