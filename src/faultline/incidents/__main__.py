"""Command line: run the incidents, or summarize a results file.

uv run python -m faultline.incidents run --trials 3
uv run python -m faultline.incidents summary
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from faultline import ida
from faultline.incidents import cluster, flagd, health, load_catalog
from faultline.incidents import records as rec
from faultline.incidents.runner import Deps, run_all, utc_now
from faultline.incidents.webhook import AlertInbox, Listener
from faultline.kube_tools import kube_api
from faultline.settings import get_settings

RUNS = Path("runs")
# The kind Docker network's gateway: the VM as the cluster's pods see it.
DEFAULT_HOST = "172.18.0.1"
DEFAULT_PORT = 9099


def git_sha() -> str:
    """HEAD, marked -dirty when the working tree differs, so a result names its code."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        sha = git("rev-parse", "HEAD")
        return sha + ("-dirty" if git("status", "--porcelain") else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def newest_results() -> Path | None:
    files = sorted(RUNS.glob("incidents-*.jsonl"))
    return files[-1] if files else None


async def run_command(args: argparse.Namespace) -> int:
    os.environ["PROVIDER"] = args.provider
    get_settings()  # fail now, not at the first agent run, when the key is missing
    catalog = load_catalog()
    out = args.out or RUNS / f"incidents-{datetime.now(UTC):%Y-%m-%d}.jsonl"
    inbox = AlertInbox()
    async with kube_api() as api, Listener(inbox, args.host, args.port):
        deps = Deps(
            wait_healthy=lambda timeout: health.wait_healthy(
                partial(health.check_health, api), timeout
            ),
            inject=partial(flagd.inject, api),
            recover_all=partial(flagd.recover_all, api),
            diagnose=lambda question, trace_file: ida.diagnose_async(
                question, trace_file=trace_file, trace_content=True
            ),
            reset_traces=partial(cluster.reset_traces, api),
            sleep=asyncio.sleep,
            inbox=inbox,
            now=utc_now,
            sha=git_sha(),
            trace_dir=RUNS / "traces" / out.stem,
            setup_timeout=args.setup_timeout,
            alert_timeout=args.alert_timeout,
        )
        await run_all(deps, catalog, out, args.trials, args.only, log=log)
    print(rec.summarize(rec.load(out), catalog.control.name))
    return 0


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {message}", file=sys.stderr, flush=True)


def summary_command(args: argparse.Namespace) -> int:
    path = args.file or newest_results()
    if path is None or not path.exists():
        print(
            "no results file: pass --file or run the incidents first", file=sys.stderr
        )
        return 1
    print(f"{path}")
    print(rec.summarize(rec.load(path), load_catalog().control.name))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m faultline.incidents")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="inject each incident, ask the agent, recover")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument(
        "--only", type=lambda s: s.split(","), help="comma-separated names"
    )
    run.add_argument("--out", type=Path, help="results file (default: dated, in runs/)")
    run.add_argument("--provider", default="anthropic", choices=["anthropic", "groq"])
    run.add_argument("--host", default=DEFAULT_HOST, help="address the listener binds")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--setup-timeout", type=float, default=1200.0)
    run.add_argument("--alert-timeout", type=float, default=1200.0)

    summary = sub.add_parser("summary", help="accuracy per incident and overall")
    summary.add_argument("--file", type=Path)

    args = parser.parse_args(argv)
    if args.command == "summary":
        return summary_command(args)
    if args.trials < 1:
        parser.error("--trials must be at least 1")
    try:
        return asyncio.run(run_command(args))
    except KeyboardInterrupt:
        print("interrupted: flags were turned off; rerun to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
