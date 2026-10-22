import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from faultline.ida import RunResult
from faultline.incidents import load_catalog
from faultline.incidents import records as rec
from faultline.incidents.catalog import Incident
from faultline.incidents.health import Health
from faultline.incidents.runner import Deps, SetupAborted, plan, run_all, run_trial
from faultline.incidents.webhook import AlertInbox, Notification

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
CART = Incident(
    name="cart_service_failure",
    flag="cartFailure",
    variant="on",
    service="cart",
    source="aiopslab/x.py",
)


def firing(name="CheckoutErrorRate", starts=T0 + timedelta(seconds=1), fp="f1"):
    return Notification.model_validate(
        {
            "version": "4",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": name, "service_name": "checkout"},
                    "annotations": {"summary": "checkout fails 40% of orders"},
                    "fingerprint": fp,
                    "startsAt": starts.isoformat(),
                }
            ],
        }
    )


class World:
    """A fake test bed: records the calls and lets a test choose what happens."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[str] = []
        self.inbox = AlertInbox()
        self.health = Health()
        self.page: Notification | None = firing()  # what injecting makes fire
        self.answer = "cart cannot reach redis.\nROOT CAUSE SERVICE: cart"
        self.inject_error: Exception | None = None
        self.reset_error: Exception | None = None
        self.diagnose_error: BaseException | None = None
        self.hang_diagnose = False
        self.questions: list[str] = []
        self.tmp = tmp_path

    async def wait_healthy(self, timeout: float) -> Health:
        self.calls.append("healthy?")
        return self.health

    async def inject(self, incident: Incident) -> None:
        self.calls.append(f"inject {incident.flag}")
        if self.inject_error:
            raise self.inject_error
        if self.page:
            self.inbox.accept(self.page)

    async def reset_traces(self) -> None:
        self.calls.append("reset traces")
        if self.reset_error:
            raise self.reset_error

    async def sleep(self, seconds: float) -> None:
        self.calls.append(f"sleep {seconds:g}")

    async def recover_all(self) -> dict[str, str]:
        self.calls.append("recover_all")
        return {}

    async def diagnose(self, question: str, trace_file: Path) -> RunResult:
        self.calls.append("diagnose")
        self.questions.append(question)
        if self.hang_diagnose:
            await asyncio.Event().wait()
        if self.diagnose_error:
            raise self.diagnose_error
        return RunResult(self.answer, "done", 4, 1000, 7.5, 600)

    def deps(self, alert_timeout=0.05) -> Deps:
        return Deps(
            wait_healthy=self.wait_healthy,
            inject=self.inject,
            recover_all=self.recover_all,
            diagnose=self.diagnose,
            reset_traces=self.reset_traces,
            sleep=self.sleep,
            inbox=self.inbox,
            now=lambda: T0,
            sha="abc123",
            trace_dir=self.tmp / "traces",
            setup_timeout=1.0,
            alert_timeout=alert_timeout,
            control_delay=300.0,
        )


def trial(world: World, incident: Incident | None = CART, **kw):
    name = incident.name if incident else "no_op"
    return asyncio.run(run_trial(world.deps(**kw), name, 1, incident))


def test_a_correct_trial_records_everything_and_cleans_up(tmp_path):
    world = World(tmp_path)

    r = trial(world)

    assert r.outcome == "correct" and r.correct
    assert (r.service, r.expected, r.alert) == ("cart", "cart", "CheckoutErrorRate")
    assert (r.stop_reason, r.steps, r.tokens, r.cache_read_tokens) == (
        "done",
        4,
        1000,
        600,
    )
    assert r.seconds == 7.5 and r.sha == "abc123" and r.recovery == "ok"
    assert r.trace_file == str(tmp_path / "traces" / "cart_service_failure-1.jsonl")
    assert world.calls == [
        "recover_all",
        "healthy?",
        "reset traces",
        "inject cartFailure",
        "diagnose",
        "recover_all",
        "healthy?",
    ]


def test_the_question_comes_from_the_alert_alone(tmp_path):
    world = World(tmp_path)

    trial(world)

    question = world.questions[0]
    assert (
        "CheckoutErrorRate" in question and "checkout fails 40% of orders" in question
    )
    for leak in ("cart", "cartFailure", "flag", "inject", "cart_service_failure"):
        assert leak not in question.replace("checkout", "")


def test_a_wrong_service_and_a_missing_line_are_told_apart(tmp_path):
    world = World(tmp_path)
    world.answer = "x\nROOT CAUSE SERVICE: checkout"
    assert trial(world).outcome == "wrong"

    world.answer = "The cart is broken."
    r = trial(world)
    assert (r.outcome, r.service, r.correct) == ("no verdict", None, False)


def test_no_alert_skips_the_agent_and_still_recovers(tmp_path):
    world = World(tmp_path)
    world.page = None

    r = trial(world)

    assert r.outcome == "no alert" and "diagnose" not in world.calls
    assert world.calls[-2:] == ["recover_all", "healthy?"]
    assert r.question is None


def test_an_unhealthy_test_bed_means_setup_failed_and_no_injection(tmp_path):
    world = World(tmp_path)
    world.health = Health(firing=["FrontendErrorRate"])

    r = trial(world)

    assert r.outcome == "setup failed"
    assert "FrontendErrorRate" in r.detail
    assert not any(c.startswith("inject") for c in world.calls)


def test_traces_are_reset_before_injecting_and_a_failed_reset_is_setup_failed(tmp_path):
    world = World(tmp_path)
    world.reset_error = RuntimeError("jaeger stuck")

    r = trial(world)

    assert r.outcome == "setup failed" and "jaeger stuck" in r.detail
    assert not any(c.startswith("inject") for c in world.calls)


def test_a_failed_injection_is_setup_failed_and_flags_are_cleared(tmp_path):
    world = World(tmp_path)
    world.inject_error = RuntimeError("flagd-ui down")

    r = trial(world)

    assert r.outcome == "setup failed" and "flagd-ui down" in r.detail
    assert world.calls[-1] == "recover_all"


def test_a_crashing_agent_is_a_result_and_the_bed_is_recovered(tmp_path):
    world = World(tmp_path)
    world.diagnose_error = RuntimeError("mcp server died")

    r = trial(world)

    assert r.outcome == "agent error" and r.stop_reason == "exception"
    assert "mcp server died" in r.detail
    assert world.calls[-2:] == ["recover_all", "healthy?"]


def test_a_cancelled_trial_still_turns_the_flags_off(tmp_path):
    world = World(tmp_path)
    world.hang_diagnose = True

    async def go():
        task = asyncio.create_task(run_trial(world.deps(), "x", 1, CART))
        while "diagnose" not in world.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert world.calls[-1] == "recover_all"


def test_a_bed_that_does_not_come_back_is_written_on_the_record(tmp_path):
    world = World(tmp_path)
    healthy, sick = Health(), Health(not_ready=["cart (not ready)"])
    readings = [healthy, sick]

    async def wait_healthy(timeout):
        return readings.pop(0) if len(readings) > 1 else readings[0]

    deps = world.deps()
    deps.wait_healthy = wait_healthy

    r = asyncio.run(run_trial(deps, CART.name, 1, CART))

    assert r.outcome == "correct"
    assert r.recovery.startswith("not healthy") and "cart (not ready)" in r.recovery


def test_a_page_for_an_earlier_alert_is_not_this_trials_page(tmp_path):
    world = World(tmp_path)
    world.page = firing(starts=T0 - timedelta(minutes=5), fp="old")

    r = trial(world)

    assert r.outcome == "no alert"


def test_the_control_gets_a_false_alarm_and_is_right_to_say_none(tmp_path):
    world = World(tmp_path)
    world.answer = "Nothing wrong.\nROOT CAUSE SERVICE: none"

    r = trial(world, incident=None)

    assert r.outcome == "correct" and r.expected == "none"
    assert not any(c.startswith("inject") for c in world.calls)
    assert r.alert == "FrontendErrorRate"
    # Traces are cleared, then the control waits for them to refill before it is asked.
    assert world.calls[:5] == [
        "recover_all",
        "healthy?",
        "reset traces",
        "sleep 300",
        "diagnose",
    ]


def test_the_control_is_wrong_when_the_agent_blames_a_service(tmp_path):
    world = World(tmp_path)
    world.answer = "ROOT CAUSE SERVICE: frontend"

    assert trial(world, incident=None).outcome == "wrong"


def test_plan_goes_round_by_round_with_the_control_last():
    catalog = load_catalog()

    order = plan(catalog, 2, None)

    assert len(order) == 2 * 12
    assert order[11] == ("no_op", 1) and order[12][1] == 2
    assert plan(catalog, 1, ["no_op", "cart_service_failure"]) == [
        ("cart_service_failure", 1),
        ("no_op", 1),
    ]
    with pytest.raises(ValueError, match="unknown incident"):
        plan(catalog, 1, ["nope"])


def test_run_all_writes_a_line_per_trial_and_resumes(tmp_path):
    world = World(tmp_path)
    out = tmp_path / "runs" / "incidents.jsonl"
    catalog = load_catalog()
    only = ["cart_service_failure", "no_op"]
    quiet = lambda _: None

    first = asyncio.run(run_all(world.deps(), catalog, out, 1, only, log=quiet))
    assert [r.incident for r in first] == ["cart_service_failure", "no_op"]
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [x["incident"] for x in lines] == ["cart_service_failure", "no_op"]
    assert {
        "sha",
        "incident",
        "trial",
        "alert",
        "question",
        "answer",
        "service",
        "correct",
        "stop_reason",
        "tokens",
        "cache_read_tokens",
        "seconds",
        "trace_file",
    } <= set(lines[0])

    world.calls.clear()
    again = asyncio.run(run_all(world.deps(), catalog, out, 2, only, log=quiet))
    assert [(r.incident, r.trial) for r in again] == [
        ("cart_service_failure", 2),
        ("no_op", 2),
    ]
    assert len(out.read_text().splitlines()) == 4


def test_a_failed_setup_is_tried_again_on_resume(tmp_path):
    world = World(tmp_path)
    out = tmp_path / "incidents.jsonl"
    catalog = load_catalog()
    only = ["cart_service_failure"]
    quiet = lambda _: None
    world.health = Health(firing=["x"])
    asyncio.run(run_all(world.deps(), catalog, out, 1, only, log=quiet))
    world.health = Health()

    again = asyncio.run(run_all(world.deps(), catalog, out, 1, only, log=quiet))

    assert [r.outcome for r in again] == ["correct"]
    records = rec.load(out)
    assert len(records) == 2
    assert rec.latest(records)[("cart_service_failure", 1)].outcome == "correct"


def test_a_torn_last_line_is_dropped_but_a_torn_middle_line_is_an_error(tmp_path):
    world = World(tmp_path)
    out = tmp_path / "incidents.jsonl"
    asyncio.run(
        run_all(world.deps(), load_catalog(), out, 1, ["no_op"], log=lambda _: None)
    )
    good = out.read_text()

    out.write_text(good + '{"sha": "abc", "incid')
    assert len(rec.load(out)) == 1

    out.write_text('{"sha": "abc", "incid\n' + good)
    with pytest.raises(ValueError, match="not a result line"):
        rec.load(out)


def test_the_summary_counts_each_kind_and_leaves_the_control_out_of_the_total():
    def r(incident, trial, outcome):
        return rec.Record("sha", incident, trial, outcome, "t", "x")

    records = [
        r("cart", 1, "correct"),
        r("cart", 2, "wrong"),
        r("cart", 3, "no alert"),
        r("ad", 1, "no verdict"),
        r("ad", 2, "setup failed"),
        r("no_op", 1, "correct"),
        r("no_op", 1, "wrong"),  # a retry: the newest line wins
    ]

    text = rec.summarize(records, "no_op")

    lines = {line.split()[0]: line for line in text.splitlines()}
    assert lines["cart"].split()[1:] == ["1/2", "0/2", "1", "0", "1", "0", "0"]
    assert lines["ad"].split()[1:] == ["0/1", "0/1", "0", "1", "0", "1", "0"]
    assert lines["no_op"].split()[2:5] == ["0/1", "0/1", "1"]
    assert lines["overall"].split()[3:6] == ["1/3", "0/3", "1"]


def test_a_cancel_while_the_bed_recovers_keeps_the_answered_trial(tmp_path):
    world = World(tmp_path)
    saved: list[rec.Record] = []
    waits = 0

    async def wait_healthy(timeout):
        nonlocal waits
        waits += 1
        if waits == 2:  # the first is the setup check, the second is the recovery
            await asyncio.Event().wait()
        return Health()

    deps = world.deps()
    deps.wait_healthy = wait_healthy

    async def go():
        task = asyncio.create_task(run_trial(deps, CART.name, 1, CART, saved.append))
        while waits < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert len(saved) == 1
    assert saved[0].outcome == "correct" and saved[0].answer
    assert saved[0].recovery.startswith("interrupted")


def test_two_setup_failures_in_a_row_stop_the_run_and_keep_the_results(tmp_path):
    world = World(tmp_path)
    world.health = Health(firing=["FrontendErrorRate"])
    out = tmp_path / "incidents.jsonl"

    with pytest.raises(SetupAborted, match=r"2 trials in a row.*--out .*incidents"):
        asyncio.run(
            run_all(world.deps(), load_catalog(), out, 1, None, log=lambda _: None)
        )

    assert [r.outcome for r in rec.load(out)] == ["setup failed"] * 2


def test_a_good_trial_between_setup_failures_resets_the_count(tmp_path):
    world = World(tmp_path)
    healths = iter([Health(firing=["x"]), Health(), Health(), Health(firing=["x"])])
    quiet = lambda _: None

    async def wait_healthy(timeout):
        return next(healths, Health())

    deps = world.deps()
    deps.wait_healthy = wait_healthy
    only = ["cart_service_failure", "ad_service_failure"]

    # fail, ok (setup + recovery), fail: never two failures in a row
    ran = asyncio.run(
        run_all(deps, load_catalog(), tmp_path / "o.jsonl", 2, only, log=quiet)
    )

    assert [r.outcome for r in ran][:3] == ["setup failed", "correct", "setup failed"]


def test_the_summary_lists_only_the_code_that_made_the_counted_results():
    def r(sha, outcome):
        return rec.Record(sha, "cart", 1, outcome, "t", "cart")

    records = [r("old-sha", "setup failed"), r("new-sha", "correct")]

    assert rec.summarize(records, "no_op").splitlines()[-1] == "git: new-sha"


def test_the_label_baseline_scores_an_agent_that_only_reads_the_page():
    def r(incident, expected, label, outcome="correct"):
        question = f"The alert A is firing. Labels: severity=page, service_name={label}. Find the cause."
        return rec.Record("sha", incident, 1, outcome, "t", expected, question=question)

    records = [
        r("cart_service_failure", "cart", "cart"),  # the label gives it away
        r("payment_service_failure", "payment", "checkout"),  # the label misleads
        r("kafka_queue_problems", "kafka", "product-catalog"),
        r("no_op", "none", "frontend"),  # a false alarm: the label is always wrong
        rec.Record("sha", "ad_service_failure", 1, "no alert", "t", "ad"),
    ]

    text = rec.summarize(records, "no_op", {"payment_service_failure": "key differs"})

    lines = {line.split()[0]: line for line in text.splitlines()}
    assert "label baseline" in text.splitlines()[0]
    assert lines["cart_service_failure"].split()[1:3] == ["1/1", "1/1"]
    assert lines["payment_service_failure*"].split()[1:3] == ["1/1", "0/1"]
    assert lines["kafka_queue_problems"].split()[2] == "0/1"
    assert lines["no_op"].split()[2:4] == ["1/1", "0/1"]
    assert lines["ad_service_failure"].split()[1:3] == ["-", "-"]
    assert lines["overall"].split()[3:5] == ["3/3", "1/3"]
    assert "* payment_service_failure: key differs" in text


def test_the_label_is_read_from_the_stored_question():
    question = "x. Labels: severity=page, service_name=product-catalog. Find the cause."

    assert rec.label_service(question) == "product-catalog"
    assert rec.label_service("The alert A is firing. Find the cause.") is None
    assert rec.label_service(None) is None


def test_the_shipped_catalog_flags_the_two_keys_that_disagree_with_the_prompt():
    assert set(load_catalog().notes()) == {
        "payment_service_unreachable",
        "loadgenerator_flood_homepage",
    }
