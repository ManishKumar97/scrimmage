"""Threads the SDK makes inside the team's program, deaf to the stop signals.

The SDK records a call in flight from its SIGTERM handler, which Python runs on the main
thread only. Linux gives a signal sent to a process to any thread that does not block it,
and passes over a main thread that is off the CPU with a signal already pending. A signal
given to one of the SDK's threads would leave the main thread blocked in its call, with no
handler run, until SIGKILL. So each SDK thread blocks SIGINT and SIGTERM.

A new thread takes its creator's signal mask, so the mask is set around start(): the
thread is deaf from its first instruction. A signal that comes while the creator has them
blocked waits, and is delivered once the mask is put back.

The runner does not use this. Its threads start the team's programs, which would take the
blocked mask with them, and its wakeup fd sees a signal on any thread.
"""

import signal
import threading

STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}


def start(thread: threading.Thread) -> None:
    """Start an SDK thread with SIGINT and SIGTERM blocked in it. Where the platform has
    no signal masks, as on Windows, the thread starts as it is."""
    if not hasattr(signal, "pthread_sigmask"):
        thread.start()
        return
    before = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        thread.start()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
