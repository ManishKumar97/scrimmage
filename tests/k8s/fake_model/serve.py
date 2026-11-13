"""The fake OpenAI API as a server in a pod: `serve.py FIXTURE...`, one fixture per turn.

It runs the same FakeOpenAI the other tests use, on a fixed port, and answers only from the
fixture files in the image. This is test support code; it is never in a wheel.
"""

import signal
import sys
import threading
from typing import Any

from fakes.openai_server import FakeOpenAI

PORT = 8080


def main(replies: list[str]) -> None:
    fake = FakeOpenAI(replies, host="0.0.0.0", port=PORT)
    fake.start()
    stop = threading.Event()

    def on_signal(signum: int, frame: Any) -> None:
        del signum, frame
        stop.set()  # as PID 1 the process gets no default action for SIGTERM

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"fake model on port {PORT}, fixtures {replies}", flush=True)
    stop.wait()
    fake.stop()


if __name__ == "__main__":
    main(sys.argv[1:])
