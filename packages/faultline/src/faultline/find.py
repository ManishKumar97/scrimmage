"""`faultline find`: read traces and report what went wrong in them."""

import argparse
from collections.abc import Sequence

from faultline.runs import show_reading
from faultline.sources import parse_source


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("find", help="read traces and print the runs")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="SPEC",
        help="where traces come from, for example file:traces/ (repeatable)",
    )
    parser.add_argument("files", nargs="*", help="shorthand for --source file:FILE")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    specs: Sequence[str] = [*args.source, *(f"file:{f}" for f in args.files)]
    if not specs:
        print("faultline find: give --source SPEC or at least one file")
        return 2
    try:
        sources = [parse_source(spec) for spec in specs]
    except ValueError as error:
        print(f"faultline find: {error}")
        return 2
    for source in sources:
        for label, reading in source.readings():
            print(show_reading(label, reading))
    return 0
