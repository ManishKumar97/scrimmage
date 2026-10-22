import asyncio
import json

import aiohttp

from faultline.incidents.webhook import (
    AlertInbox,
    Listener,
    Notification,
    build_question,
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


def payload(*alerts, version="4", status="firing"):
    return {
        "version": version,
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
