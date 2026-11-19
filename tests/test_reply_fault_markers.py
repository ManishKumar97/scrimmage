"""What a run keeps for its reply faults, and what the check on each request costs, when every
model call in a long run is faulted."""

import gc
import json
import time
import tracemalloc

from scrimmage_core import provider_openai as openai_table
from scrimmage_core import state
from scrimmage_core.plan import Plan
from test_http_gate import Sink

LABEL = "model:openai"
TURNS = 300
USER = "u" * 2000  # 2 KB from the user each turn
REPLY = "r" * 500  # and a 500-byte reply
MAX_KEPT = 5 * 1024 * 1024  # the goal is under 1 MB; the bound leaves room for a busy machine
MAX_CHECK_S = 0.050  # the goal is under 10 ms


def sent(messages: list[dict[str, object]]) -> dict[str, object]:
    """A chat request as the gate reads it: its body parsed fresh, as from a real request."""
    body = json.dumps({"model": "gpt-4o-mini", "messages": messages}).encode()
    return openai_table.request_fields(body) or {}


def faulted_run() -> tuple[state.RunState, list[dict[str, object]]]:
    """A run where every one of TURNS model calls got a faulted reply, and the conversation
    the agent built from those replies."""
    run = state.RunState(Plan("run-1", "t", "baseline", (), (), (), (), 60.0), Sink(), [])
    messages: list[dict[str, object]] = [{"role": "system", "content": "You help."}]
    for turn in range(TURNS):
        messages.append({"role": "user", "content": f"{turn} {USER}"})
        fields = sent(messages)
        reply = {"role": "assistant", "content": f"{turn} {REPLY}", "refusal": None}
        built = openai_table.chat_reply(fields, 0, reply, "stop")
        run.add_reply_fault_markers(LABEL, openai_table.given(fields, built, ids_only=False))
        messages.append({"role": "assistant", "content": reply["content"]})
    messages.append({"role": "user", "content": f"next {USER}"})
    return run, messages


def check_time(run: state.RunState, fields: dict[str, object]) -> tuple[bool, float]:
    """What one check on this request says, and the least of three times it takes."""
    times: list[float] = []
    carried = False
    for _ in range(3):
        start = time.perf_counter()
        carried = run.carries_reply_fault(LABEL, fields)
        times.append(time.perf_counter() - start)
    return carried, min(times)


def test_markers_for_a_long_run_with_every_reply_faulted_stay_small_and_quick() -> None:
    gc.collect()
    tracemalloc.start()
    try:
        run, _ = faulted_run()
        gc.collect()
        with_markers = tracemalloc.get_traced_memory()[0]
        run.reply_fault_markers.clear()
        gc.collect()
        kept = with_markers - tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    run, messages = faulted_run()
    carried, carrying_s = check_time(run, sent(messages))
    # The same conversation under another system message carries none of the replies, so
    # the check compares it with every kept prefix: the slowest case.
    other = [{"role": "system", "content": "You help well."}, *messages[1:]]
    other_carried, other_s = check_time(run, sent(other))
    print(
        f"markers kept {kept / 1e6:.3f} MB; one check {carrying_s * 1e3:.2f} ms carrying, "
        f"{other_s * 1e3:.2f} ms not carrying"
    )
    assert carried
    assert not other_carried
    assert kept < MAX_KEPT, f"markers keep {kept} bytes"
    slowest = max(carrying_s, other_s)
    assert slowest < MAX_CHECK_S, f"one check takes {slowest * 1e3:.1f} ms"
