import asyncio
import os
import signal

import pytest

from faultline.incidents import __main__ as main_module
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


def test_summary_picks_the_file_written_last_not_the_last_name(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "RUNS", tmp_path)
    older, newer = (
        tmp_path / "incidents-smoke2.jsonl",
        tmp_path / "incidents-2026.jsonl",
    )
    older.write_text("")
    newer.write_text("")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    assert main_module.newest_results() == newer


def test_summary_with_no_results_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "RUNS", tmp_path)

    assert main_module.newest_results() is None
