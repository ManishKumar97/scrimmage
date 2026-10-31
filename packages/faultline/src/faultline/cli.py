"""The `faultline` command. Each subcommand lives in its own module."""

import argparse
import sys
from collections.abc import Sequence

from faultline import find, launch, serve
from faultline.proxy import mcp


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    find.add_parser(subparsers)
    serve.add_parser(subparsers)
    launch.add_parser(subparsers)
    mcp.add_parser(subparsers)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
