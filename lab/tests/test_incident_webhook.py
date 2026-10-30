import asyncio
import json
from datetime import UTC, datetime

import aiohttp

from faultline_lab.incidents.webhook import (
    AlertInbox,
    Listener,
    Notification,
    build_question,
    synthetic_notification,
)


def alert(name="FrontendErrorRate", status="firing", fingerprint="f1", **extra):
    return {
        "status": status,
        "labels": {"alertname": name, "severity": "page", "service_name": "frontend"}
        | extra.pop("labels", {}),
        "annotations": extra.pop("annotations", {"summary": "5xx rate is 4%"}),
        "fingerprint": fingerprint,
        "startsAt": "2026-10-21T12:00:00Z",
        **extra,
    }


def payload(*alerts, status="firing"):
    return {
        "version": "4",
        "status": status,
        "receiver": "faultline",
        "groupKey": "{}:{}",
        "alerts": list(alerts),
    }


def page(inbox: AlertInbox):
    return inbox.queue.get_nowait()


def post(body, raw: bytes | None = None, inbox: AlertInbox | None = None):
    """POST to a real listener on the loopback. Return (status, json, inbox)."""
    inbox = inbox or AlertInbox()

    async def go():
        async with Listener(inbox, "127.0.0.1", 0) as listener:
            url = f"http://127.0.0.1:{listener.bound_port}/alerts"
            async with aiohttp.ClientSession() as session:
                data = raw if raw is not None else json.dumps(body).encode()
                async with session.post(url, data=data) as response:
                    text = await response.text()
                    return response.status, json.loads(text) if response.ok else text

    status, answer = asyncio.run(go())
    return status, answer, inbox


def test_a_firing_alert_is_queued_and_a_repeat_is_not():
    status, answer, inbox = post(payload(alert()))
    assert (status, answer) == (200, {"new": 1})

    status, answer, _ = post(payload(alert()), inbox=inbox)
    assert (status, answer) == (200, {"new": 0})
    assert inbox.queue.qsize() == 1


def test_resolved_alerts_are_ignored():
    status, answer, inbox = post(payload(alert(status="resolved"), status="resolved"))

    assert (status, answer) == (200, {"new": 0})
    assert inbox.queue.empty()


def test_a_group_hands_on_one_page_and_names_every_alert():
    both = payload(
        alert("ZLatency", fingerprint="f2"), alert("AErrors", fingerprint="f1")
    )
    _, answer, inbox = post(both)

    assert answer == {"new": 2}
    first, names = page(inbox)
    assert first.name == "AErrors"  # a fixed order, not the arrival order
    assert names == ["AErrors", "ZLatency"]


def test_junk_is_rejected_and_queues_nothing():
    for raw in (b"not json", b"{}", b'{"version":"4"}', b"[]"):
        status, _, inbox = post(None, raw=raw)
        assert status == 400, raw
        assert inbox.queue.empty()
    bad_alert = payload({"status": "firing", "labels": {}, "annotations": {}})
    status, _, inbox = post(bad_alert)
    assert status == 400 and inbox.queue.empty()


def test_the_listener_frees_its_port_on_exit():
    async def go():
        inbox = AlertInbox()
        async with Listener(inbox, "127.0.0.1", 0) as first:
            port = first.bound_port
        async with Listener(inbox, "127.0.0.1", port):
            return port

    assert asyncio.run(go()) > 0


def test_the_question_uses_only_the_alert():
    note = Notification.model_validate(
        payload(
            alert(
                labels={"job": "x", "instance": "10.0.0.1"},
                annotations={"summary": "5xx rate is 4%", "runbook": "see wiki"},
            )
        )
    )

    question = build_question(note.alerts[0])

    assert question == (
        "The alert FrontendErrorRate is firing in the Astronomy Shop. "
        "5xx rate is 4%. Labels: severity=page, service_name=frontend. "
        "Find the cause."
    )
    assert "10.0.0.1" not in question and "wiki" not in question


def test_reset_forgets_handled_alerts_and_unread_pages():
    inbox = AlertInbox()
    note = Notification.model_validate(payload(alert()))
    assert inbox.accept(note) == 1

    inbox.reset()

    assert inbox.queue.empty()
    assert inbox.accept(note) == 1


def test_a_synthetic_page_goes_through_the_same_inbox():
    inbox = AlertInbox()
    note = synthetic_notification(
        {"alertname": "FrontendErrorRate"},
        "5xx rate is 4%",
        datetime(2026, 10, 21, tzinfo=UTC),
    )

    assert inbox.accept(note) == 1
    first, _ = page(inbox)
    assert "FrontendErrorRate" in build_question(first)


def test_a_missing_or_zoneless_start_time_is_refused_not_a_crash():
    no_start = alert()
    del no_start["startsAt"]
    for bad in (no_start, alert(startsAt="2026-10-21T12:00:00"), alert(startsAt=None)):
        # The good alert rides in the same batch: one bad alert refuses the batch.
        status, _, inbox = post(payload(alert(fingerprint="ok"), bad))
        assert status == 400 and inbox.queue.empty()


def test_a_stale_alert_is_dropped_and_does_not_hide_a_later_real_page():
    inbox = AlertInbox()
    inbox.reset(since=datetime(2026, 10, 21, 12, 5, tzinfo=UTC))
    stale = alert("AStale", fingerprint="s", startsAt="2026-10-21T12:00:00Z")
    fresh = alert("ZFresh", fingerprint="z", startsAt="2026-10-21T12:06:00Z")

    # The stale alert sorts first, so it would be the page if it were not dropped.
    assert inbox.accept(Notification.model_validate(payload(stale, fresh))) == 1
    first, names = page(inbox)
    assert (first.name, names) == ("ZFresh", ["ZFresh"])

    again = alert("AStale", fingerprint="s", startsAt="2026-10-21T12:07:00Z")
    assert inbox.accept(Notification.model_validate(payload(again))) == 1


def test_the_route_label_reaches_the_question():
    note = Notification.model_validate(
        payload(
            alert(
                "CheckoutRouteErrorRate",
                labels={"service_name": "frontend-proxy", "route": "checkout"},
                annotations={"summary": "checkout fails 100% of its requests"},
            )
        )
    )

    question = build_question(note.alerts[0])

    assert "route=checkout" in question
