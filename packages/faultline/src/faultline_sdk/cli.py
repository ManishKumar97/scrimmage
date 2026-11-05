"""The faultline command. This version has one command: faultline test."""

import argparse
import sys
from pathlib import Path

from faultline_sdk import config as settings
from faultline_sdk.errors import ConfigError
from faultline_sdk.runner import INFRA, MISTAKE, Interrupted, Runner, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultline")
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="run every test in faultline.toml")
    test.add_argument("path", nargs="?", default="faultline.toml", type=Path)
    args = parser.parse_args(argv)
    try:
        config = settings.load(args.path.resolve())
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"faultline: {problem}", file=sys.stderr)
        print("faultline: the test stopped before the first run", file=sys.stderr)
        return MISTAKE
    runner: Runner | None = None
    try:
        runner = Runner(config)
        results = runner.run_all()
    except Interrupted as exc:
        print(f"faultline: stopped by {exc}; every run was stopped", file=sys.stderr)
        return INFRA
    except Exception as exc:  # the runner's own failure is never a result
        print(f"faultline: infrastructure error in the runner: {exc!r}", file=sys.stderr)
        return INFRA
    finally:
        if runner is not None:
            runner.server.server.server_close()
    assert runner is not None
    return report(config, results, runner.folder, sys.stdout, runner)


if __name__ == "__main__":
    sys.exit(main())
