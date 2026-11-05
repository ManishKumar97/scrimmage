"""The faultline command. This version has one command: faultline test."""

import argparse
import sys
from pathlib import Path

from faultline_sdk import config as settings
from faultline_sdk.errors import ConfigError
from faultline_sdk.runner import MISTAKE, Runner, check_labels, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultline")
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="run every test in faultline.toml")
    test.add_argument("path", nargs="?", default="faultline.toml", type=Path)
    args = parser.parse_args(argv)
    try:
        config = settings.load(args.path.resolve())
    except FileNotFoundError:
        print(f"faultline: {args.path} not found", file=sys.stderr)
        return MISTAKE
    except ConfigError as exc:
        print(f"faultline: {exc}", file=sys.stderr)
        return MISTAKE
    problems = check_labels(config)
    if problems:
        for problem in problems:
            print(f"faultline: {problem}", file=sys.stderr)
        print("faultline: the test stopped before the first run", file=sys.stderr)
        return MISTAKE
    runner = Runner(config)
    results = runner.run_all()
    return report(config, results, runner.folder, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
