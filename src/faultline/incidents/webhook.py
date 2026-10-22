"""Receive Alertmanager pages and turn each into the question the agent is asked.

Alertmanager pushes to receivers with `webhook_configs`, the mechanism chat and paging
integrations use. This is a small receiver of that kind. It checks the payload,
ignores resolved alerts and repeats of an alert it already handed on, and builds the
question from the alert alone: its name, summary and labels.
"""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Self

from aiohttp import web
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

MAX_BODY_BYTES = 256 * 1024
MAX_VALUE_CHARS = 300  # one label or summary, cut
# Labels that identify the alert's source. Others (job, instance, pod IPs) are noise.
LABELS_SHOWN = ("alertname", "severity", "service_name")


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    fingerprint: str = Field(min_length=1)
    # Alertmanager always sends it with an offset. A page without one cannot be placed
    # in time, so it is refused instead of guessed at.
    starts_at: AwareDatetime = Field(alias="startsAt")

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unnamed alert")


class Notification(BaseModel):
    """Alertmanager's webhook payload, version 4."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: str
    status: str
    alerts: list[Alert]


def clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def build_question(alert: Alert) -> str:
    """The agent's question. It uses only what the alert itself says."""
    labels = ", ".join(
        f"{key}={clip(alert.labels[key])}"
        for key in LABELS_SHOWN
        if key in alert.labels and key != "alertname"
    )
    parts = [f"The alert {clip(alert.name)} is firing in the Astronomy Shop."]
    if summary := alert.annotations.get("summary"):
        parts.append(clip(summary).rstrip(".") + ".")
    if labels:
        parts.append(f"Labels: {labels}.")
    parts.append("Find the cause.")
    return " ".join(parts)


def firing(note: Notification) -> list[Alert]:
    """The firing alerts of a notification, in a fixed order."""
    return sorted(
        (a for a in note.alerts if a.status == "firing"),
        key=lambda a: (a.name, a.fingerprint),
    )


class AlertInbox:
    """Hands the first new firing alert to whoever is waiting for a page."""

    def __init__(self) -> None:
        self.handled: set[str] = set()  # fingerprints already handed on
        self.queue: asyncio.Queue[tuple[Alert, list[str]]] = asyncio.Queue()

    def accept(self, note: Notification) -> int:
        """Queue the new firing alerts. Return how many were new."""
        new = [a for a in firing(note) if a.fingerprint not in self.handled]
        if new:
            self.handled.update(a.fingerprint for a in new)
            self.queue.put_nowait((new[0], [a.name for a in new]))
        return len(new)

    def reset(self) -> None:
        """Forget what was handled and drop anything unread. Call between trials."""
        self.handled.clear()
        while not self.queue.empty():
            self.queue.get_nowait()

    async def next_alert(self, timeout: float) -> tuple[Alert, list[str]] | None:
        """The next page, or None when none comes within the timeout."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None


def make_app(inbox: AlertInbox) -> web.Application:
    async def alerts(request: web.Request) -> web.Response:
        try:
            note = Notification.model_validate_json(await request.read())
        except ValidationError:
            return web.json_response(
                {"error": "not an Alertmanager payload"}, status=400
            )
        if note.version != "4":
            return web.json_response({"error": "unsupported version"}, status=400)
        return web.json_response({"new": inbox.accept(note)})

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/alerts", alerts)
    return app


class Listener:
    """The receiver, bound to one address. Use as an async context manager."""

    def __init__(self, inbox: AlertInbox, host: str, port: int) -> None:
        self.inbox = inbox
        self.host = host
        self.port = port
        self.runner: web.AppRunner | None = None

    async def __aenter__(self) -> Self:
        self.runner = web.AppRunner(make_app(self.inbox), access_log=None)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
        except BaseException:
            await self.runner.cleanup()
            raise
        return self

    @property
    def bound_port(self) -> int:
        assert self.runner is not None
        return self.runner.addresses[0][1]

    async def __aexit__(self, *exc: object) -> None:
        if self.runner:
            await self.runner.cleanup()


def synthetic_notification(
    labels: Mapping[str, str], summary: str, starts_at: datetime
) -> Notification:
    """A page that did not come from Alertmanager. Used for the false-alarm control."""
    alert = Alert(
        startsAt=starts_at,
        status="firing",
        labels=dict(labels),
        annotations={"summary": summary},
        fingerprint="synthetic-" + labels.get("alertname", "alert"),
    )
    return Notification(version="4", status="firing", alerts=[alert])
