"""`faultline find`: read traces and report the faults in them."""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from faultline.config import load_config
from faultline.detect import detect
from faultline.model import Fault, FaultKind, Run
from faultline.runs import show_run
from faultline.sources import parse_source


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("find", help="read traces and print their faults")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="SPEC",
        help="where traces come from, for example file:traces/ (repeatable)",
    )
    parser.add_argument(
        "--config", type=Path, help="TOML settings, default ./faultline.toml if present"
    )
    parser.add_argument(
        "--runs", action="store_true", help="also print every run, not only the faults"
    )
    parser.add_argument("files", nargs="*", help="shorthand for --source file:FILE")
    parser.set_defaults(handler=run)


def show_faults(trace_id: str, faults: Sequence[Fault], width: int) -> str:
    lines = [f"run {trace_id}"]
    for fault in faults:
        where = "before any step" if fault.step is None else f"step {fault.step}"
        who = f" {fault.tool} {fault.call_id}" if fault.tool else ""
        lines.append(f"  {fault.kind:<{width}} {where}{who}: {fault.detail}")
    return "\n".join(lines)


def show_summary(faults: Sequence[Fault]) -> str:
    counts = Counter(fault.kind for fault in faults)
    shown = [f"{kind}={counts[kind]}" for kind in FaultKind if counts[kind]]
    return "faults: " + (" ".join(shown) if shown else "none")


def fail(message: str) -> int:
    print(f"faultline find: {message}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    specs: Sequence[str] = [*args.source, *(f"file:{f}" for f in args.files)]
    if not specs:
        return fail("give --source SPEC or at least one file")
    try:
        config = load_config(args.config)
        sources = [parse_source(spec, config.attributes) for spec in specs]
    except ValueError as error:  # includes ConfigError
        return fail(str(error))
    runs: list[Run] = []
    files = 0
    for source in sources:
        try:
            found = list(source.runs())
        except OSError as error:
            return fail(
                f"cannot read {error.filename or source.name}: {error.strerror}"
            )
        stats = source.stats()
        files += stats.files
        runs += found
        if args.runs:
            print(*(show_run(r) for r in found), sep="\n")
        if stats.unreadable:
            print(f"{source.name}: skipped {stats.unreadable} unreadable line(s)")
        if stats.no_spans:
            print(f"{source.name}: {stats.no_spans} line(s) held no spans")
    print(f"read {len(runs)} run(s) from {files} file(s)")
    if files == 0:
        return fail("no span files found")
    if not runs:
        return fail("read the files but found no runs; are they OTLP GenAI span files?")
    by_run = [(r.trace_id, detect(r, config.detect)) for r in runs]
    faults = [fault for _, found in by_run for fault in found]
    width = max((len(fault.kind) for fault in faults), default=0)
    for trace_id, found in by_run:
        if found:
            print(show_faults(trace_id, found, width))
    print(show_summary(faults))
    return 0
