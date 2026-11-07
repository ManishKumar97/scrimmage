"""The derail command's entry point. With --json, it takes SIGINT and SIGTERM first.

Derail's own modules take a few hundred milliseconds to import, which is most of a
`derail check`. A signal in that time must still end in one JSON document, so this module
imports only the standard library, notes a signal that arrives while the rest loads, and
leaves it to derail_sdk.cli, which stops at once with the document.
"""

import signal
import sys

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
pending: list[str] = []  # the first signal that came before cli could act on it


def note(signum: int, frame: object) -> None:
    del frame
    if not pending:
        pending.append(signal.Signals(signum).name)


def main() -> None:
    if "--json" in sys.argv[1:]:
        for sig in STOP_SIGNALS:
            signal.signal(sig, note)
    from derail_sdk import cli  # the heavy import, now under the handlers above

    sys.exit(cli.main(process=True))
