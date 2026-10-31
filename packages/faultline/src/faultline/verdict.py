"""The verdict on one run: passed, failed loudly or failed silently.

Rules, in order, with no model or person judging the run:

1. A non-zero exit, a run that ended in error, or no answer: failed loudly.
2. Otherwise, against the expected text: an answer that contains it passed, an
   answer that lacks it failed silently.

Without an expected text there is no reference, so a run that gets past rule 1 has no
verdict. A clean run as the reference comes with `faultline test`.
"""

from faultline.model import Run, RunVerdict, Verdict

NO_REFERENCE = "no reference: pass --expect or use faultline test (P3.4)"


def injected(run: Run | None) -> list[str]:
    """The fault specs the proxy applied in the run, in the order they were applied."""
    if run is None:
        return []
    found = [step.injected for step in run.steps]
    found += [call.injected for call in run.tool_calls]
    return list(dict.fromkeys(spec for spec in found if spec))


def judge(
    run: Run | None, trace_id: str, exit_code: int, expect: str | None
) -> RunVerdict | None:
    """The verdict, or None when the run got past rule 1 and there is no reference."""
    faults = injected(run)
    if exit_code:
        verdict, reason = Verdict.FAILED_LOUDLY, f"exit code {exit_code}"
    elif run is None:
        verdict, reason = Verdict.FAILED_LOUDLY, "no model call recorded"
    elif run.error_type:
        verdict, reason = Verdict.FAILED_LOUDLY, f"error {run.error_type}"
    elif not run.answer:
        verdict, reason = Verdict.FAILED_LOUDLY, "no answer"
    elif expect is None:
        return None
    elif expect in run.answer:
        verdict, reason = Verdict.PASSED, f'answer has "{expect}"'
    else:
        verdict, reason = Verdict.FAILED_SILENTLY, f'answer lacks "{expect}"'
    if faults:
        reason += f"; injected {', '.join(faults)}"
    return RunVerdict(trace_id, verdict, reason, ", ".join(faults) or None)


def line(verdict: RunVerdict | None) -> str:
    """The one line `faultline run` prints after the agent exits."""
    if verdict is None:
        return NO_REFERENCE
    return f"verdict: {verdict.verdict.value} ({verdict.reason})"
