import asyncio
import os
import signal

import pytest

from faultline.incidents.__main__ import Terminated, main, stop_on_signals


def test_run_needs_an_explicit_results_file(capsys):
    # A dated default would start over in a new empty file when a resume crossed midnight.
    with pytest.raises(SystemExit) as stop:
        main(["run", "--trials", "1"])

    assert stop.value.code == 2
    assert "--out" in capsys.readouterr().err


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_unwinds_through_cleanup_like_ctrl_c(signum):
    cleaned = []

    async def work() -> int:
        try:
            os.kill(os.getpid(), signum)
            await asyncio.sleep(30)  # the signal cancels this long before it ends
            return 0
        finally:
            await asyncio.sleep(0)  # cleanup can still await, as recover_all does
            cleaned.append("flags off")

    with pytest.raises(Terminated) as stop:
        asyncio.run(stop_on_signals(work()))

    assert cleaned == ["flags off"]
    assert stop.value.signum == signum


def test_the_signal_handlers_are_removed_afterwards():
    async def work() -> int:
        return 7

    assert asyncio.run(stop_on_signals(work())) == 7
    assert signal.getsignal(signal.SIGTERM) in (
        signal.SIG_DFL,
        signal.default_int_handler,
    )
