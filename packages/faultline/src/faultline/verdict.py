"""The verdict on one run: passed, failed loudly or failed silently.

Rules, in order, with no model or person judging the run:

1. A non-zero exit, a run that ended in error, or no answer: failed loudly.
2. Otherwise, against the expected text: an answer that contains it passed, an
   answer that lacks it failed silently.

Without an expected text there is no reference, so a run that gets past rule 1 has no
verdict. Nor has a run whose last reply was a stream the proxy could not read: its
answer is not in the trace, which is no fault of the agent's. A clean run as the reference comes with `faultline test`.
"""

from faultline.model import Run, RunVerdict, Verdict

NO_REFERENCE = "no reference: pass --expect or use faultline test (P3.4)"
STREAMED = "no verdict: answer not recorded (streamed reply)"


def injected(run: Run | None) -> list[str]:
    """The fault specs the proxy applied in the run, in the order they were applied."""
    if run is None:
        return []
    found = [step.injected for step in run.steps]
    found += [call.injected for call in run.tool_calls]
    return list(dict.fromkeys(spec for spec in found if spec))


def judge(
    run: Run | None,
    trace_id: str,
    exit_code: int,
    expect: str | None,
    answer_unrecorded: bool = False,
) -> RunVerdict | str:
    """The verdict, or why there is none.

    `answer_unrecorded` says the last reply was a stream the proxy could not read,
    so a missing answer is the proxy's gap, not the agent's failure.
    """
    faults = injected(run)
    if exit_code:
        verdict, reason = Verdict.FAILED_LOUDLY, f"exit code {exit_code}"
    elif run is None:
        verdict, reason = Verdict.FAILED_LOUDLY, "no model call recorded"
    elif run.error_type:
        verdict, reason = Verdict.FAILED_LOUDLY, f"error {run.error_type}"
    elif not run.answer and answer_unrecorded:
        return STREAMED
    elif not run.answer:
        verdict, reason = Verdict.FAILED_LOUDLY, "no answer"
    elif expect is None:
        return NO_REFERENCE
    elif expect in run.answer:
        verdict, reason = Verdict.PASSED, f'answer has "{expect}"'
    else:
        verdict, reason = Verdict.FAILED_SILENTLY, f'answer lacks "{expect}"'
    if faults:
        reason += f"; injected {', '.join(faults)}"
    return RunVerdict(trace_id, verdict, reason, ", ".join(faults) or None)


def line(verdict: RunVerdict | str) -> str:
    """The one line `faultline run` prints after the agent exits."""
    if isinstance(verdict, str):
        return verdict
    return f"verdict: {verdict.verdict.value} ({verdict.reason})"
