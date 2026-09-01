"""Slack channel: events webhook with signature verification and a replay window.

Slack signs every request with an HMAC over ``v0:{timestamp}:{body}``. Both halves of the
check matter: the signature proves the sender, and the five-minute window on the timestamp
is what stops a captured request from being replayed later.

Slack retries an event it does not see acknowledged within three seconds, so the answer is
produced in the background and the webhook returns immediately. Answering slowly instead
would produce duplicate work and duplicate replies.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from agent_forge.api.auth import verify_slack_signature
from agent_forge.channels.openai_api import get_runtime
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AuthenticationError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

__all__ = ["router"]

router = APIRouter(prefix="/channels/slack", tags=["slack"])


class SlackEvent(BaseModel):
    type: str = ""
    text: str = ""
    user: str = ""
    channel: str = ""
    thread_ts: str = ""
    ts: str = ""
    bot_id: str = ""

    model_config = {"extra": "ignore"}

    @property
    def is_from_a_bot(self) -> bool:
        """Answering a bot is how two bots end up talking to each other forever."""
        return bool(self.bot_id)


class SlackEnvelope(BaseModel):
    type: str = "event_callback"
    challenge: str = ""
    event: SlackEvent = Field(default_factory=SlackEvent)

    model_config = {"extra": "ignore"}


@router.post("/events")
async def events(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    x_slack_signature: Annotated[str, Header()] = "",
    x_slack_request_timestamp: Annotated[str, Header()] = "",
) -> dict[str, Any]:
    """Handle a Slack event. Verifies first, answers in the background."""
    settings = runtime.channel_settings.slack
    body = await request.body()

    if not verify_slack_signature(
        settings.signing_secret, x_slack_request_timestamp, body, x_slack_signature
    ):
        log.warning("slack.signature_rejected")
        raise AuthenticationError("Slack signature failed verification")

    envelope = SlackEnvelope.model_validate(await request.json())
    if envelope.type == "url_verification":
        return {"challenge": envelope.challenge}

    event = envelope.event
    if event.type not in {"app_mention", "message"} or not event.text.strip():
        return {"ok": True}
    if event.is_from_a_bot:
        return {"ok": True, "skipped": "bot message"}

    identity = _identity_for(event, runtime, group_map=settings.group_map)
    # Answer out of band: Slack retries anything it does not see acknowledged in three
    # seconds, and a retried event means duplicated work and a duplicated reply.
    asyncio.create_task(_answer(runtime, event, identity))  # noqa: RUF006
    return {"ok": True}


def _identity_for(
    event: SlackEvent, runtime: Runtime, *, group_map: dict[str, list[str]]
) -> Identity:
    groups = tuple(group_map.get(event.user, []))
    return Identity(
        tenant_id=runtime.tenant_id,
        user_id=event.user or None,
        groups=groups,
        classification_ceiling=Classification.C2 if groups else Classification.C0,
        authenticated=bool(event.user),
    )


async def _answer(runtime: Runtime, event: SlackEvent, identity: Identity) -> None:
    thread = event.thread_ts or event.ts
    try:
        record = await runtime.tasks.ask(
            event.text, identity, thread_id=f"{event.channel}:{thread}"
        )
    except Exception as exc:  # a background task must not die silently
        log.warning("slack.answer_failed", detail=f"{type(exc).__name__}: {exc}"[:200])
        return

    text = record.answer
    if record.citations:
        text += "\n\n*Fuentes*\n" + "\n".join(f"• `{c}`" for c in record.citations)
    await _post(runtime, channel=event.channel, thread_ts=thread, text=text)


async def _post(runtime: Runtime, *, channel: str, thread_ts: str, text: str) -> None:
    token = runtime.channel_settings.slack.bot_token
    if not token:
        log.warning(
            "slack.no_bot_token",
            detail="the answer was produced but cannot be posted back",
        )
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"authorization": f"Bearer {token}"},
                json={"channel": channel, "thread_ts": thread_ts, "text": text},
            )
            payload = response.json()
    except httpx.HTTPError as exc:
        log.warning("slack.post_failed", detail=str(exc)[:200])
        return
    if not payload.get("ok"):
        log.warning("slack.post_rejected", error=str(payload.get("error"))[:100])
