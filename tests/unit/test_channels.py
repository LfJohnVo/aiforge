"""The conversational channels: WebSocket, Teams, Slack and OpenWebUI.

Every channel is the same cell behind a different door, so what these tests check is the
door: does it verify who is knocking, and does the identity it builds carry the right
ceiling. A channel that answers an unverified request is the whole vulnerability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_forge.api.auth import verify_slack_signature
from agent_forge.channels.slack import SlackEvent, _identity_for
from agent_forge.channels.teams import (
    ISSUER,
    TeamsActivity,
    identity_for,
    verify_bot_framework_token,
)
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AuthenticationError
from agent_forge.core.state import Identity
from tests.cell import API_KEY, cell_env, make_app, make_runtime
from tests.support import FakeTransport

ANSWER = "El limite es 1500 MXN."
SLACK_SECRET = "shhh-signing-secret"
TEAMS_APP_ID = "11111111-2222-3333-4444-555555555555"


def _cell(**env_extra: str) -> Any:
    return make_runtime(
        FakeTransport(replies=[ANSWER]),
        env=cell_env(**env_extra),
        enable=("teams", "slack"),
    )


# ------------------------------------------------------------------------ WebSocket


def test_a_socket_without_a_credential_is_closed_before_it_becomes_a_session() -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(make_app(_cell())) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/ws/chat"),
    ):
        pass

    assert caught.value.code == 4401


def test_a_socket_authenticated_by_query_parameter_receives_the_typed_events() -> None:
    """Browsers cannot set headers on a handshake, so the query parameter has to work."""
    with (
        TestClient(make_app(_cell())) as client,
        client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as socket,
    ):
        assert json.loads(socket.receive_text())["type"] == "ready"
        socket.send_text(json.dumps({"content": "cual es el limite?"}))
        events = _drain(socket)

    kinds = [event["type"] for event in events]
    assert kinds[-1] == "done"
    assert "token" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "token") == ANSWER


def test_a_malformed_frame_is_an_error_event_not_a_dropped_connection() -> None:
    with (
        TestClient(make_app(_cell())) as client,
        client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as socket,
    ):
        socket.receive_text()
        socket.send_text('{"content": ""}')
        event = json.loads(socket.receive_text())
        # Still usable afterwards: one bad frame must not end the conversation.
        socket.send_text(json.dumps({"content": "hola"}))
        follow_up = _drain(socket)

    assert event["type"] == "error"
    assert follow_up[-1]["type"] == "done"


def _drain(socket: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        event = json.loads(socket.receive_text())
        events.append(event)
        if event["type"] == "done":
            return events


# ---------------------------------------------------------------------------- Teams


def _rsa_token(**claims: Any) -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = {
        "iss": ISSUER,
        "aud": TEAMS_APP_ID,
        "exp": int(time.time()) + 300,
        "serviceurl": "https://smba.trafficmanager.net/mx/",
    }
    payload.update(claims)
    token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})
    return token, key.public_key()


class _StubJwks:
    """Stands in for Microsoft's JWKS endpoint."""

    def __init__(self, public_key: Any) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        del token
        return type("Key", (), {"key": self._key})()


def test_teams_refuses_every_webhook_when_the_app_id_is_unset() -> None:
    """An unset audience would accept any Bot Framework token, which is the whole attack."""
    token, public = _rsa_token()

    with pytest.raises(AuthenticationError):
        verify_bot_framework_token(f"Bearer {token}", app_id="", jwk_client=_StubJwks(public))


def test_teams_rejects_a_token_issued_for_another_bot() -> None:
    token, public = _rsa_token(aud="99999999-0000-0000-0000-000000000000")

    with pytest.raises(AuthenticationError):
        verify_bot_framework_token(
            f"Bearer {token}", app_id=TEAMS_APP_ID, jwk_client=_StubJwks(public)
        )


def test_teams_rejects_a_token_from_another_issuer() -> None:
    token, public = _rsa_token(iss="https://evil.example.com")

    with pytest.raises(AuthenticationError):
        verify_bot_framework_token(
            f"Bearer {token}", app_id=TEAMS_APP_ID, jwk_client=_StubJwks(public)
        )


def test_teams_rejects_an_expired_token() -> None:
    token, public = _rsa_token(exp=int(time.time()) - 60)

    with pytest.raises(AuthenticationError):
        verify_bot_framework_token(
            f"Bearer {token}", app_id=TEAMS_APP_ID, jwk_client=_StubJwks(public)
        )


def test_teams_rejects_a_missing_bearer_header() -> None:
    with pytest.raises(AuthenticationError):
        verify_bot_framework_token(None, app_id=TEAMS_APP_ID, jwk_client=_StubJwks(None))


def test_teams_accepts_a_token_that_checks_out() -> None:
    token, public = _rsa_token()

    claims = verify_bot_framework_token(
        f"Bearer {token}", app_id=TEAMS_APP_ID, jwk_client=_StubJwks(public)
    )

    assert claims["aud"] == TEAMS_APP_ID


def test_a_teams_user_the_directory_does_not_place_holds_no_groups_and_stays_at_c0() -> None:
    activity = TeamsActivity.model_validate(
        {"type": "message", "text": "hola", "from": {"aadObjectId": "unknown-user"}}
    )

    identity = identity_for(activity, _cell(), group_map={"someone-else": ["finanzas"]})

    assert identity.groups == ()
    assert identity.classification_ceiling == Classification.C0
    assert identity.authenticated is True


def test_a_mapped_teams_user_carries_their_groups() -> None:
    activity = TeamsActivity.model_validate(
        {"type": "message", "text": "hola", "from": {"aadObjectId": "u-1"}}
    )

    identity = identity_for(activity, _cell(), group_map={"u-1": ["finanzas"]})

    assert identity.groups == ("finanzas",)
    assert identity.classification_ceiling == Classification.C2


def test_the_stable_aad_object_id_is_preferred_over_the_per_channel_id() -> None:
    activity = TeamsActivity.model_validate(
        {"type": "message", "text": "hola", "from": {"id": "29:channel", "aadObjectId": "u-1"}}
    )

    assert activity.user_object_id == "u-1"


async def test_the_teams_webhook_answers_a_verified_activity(monkeypatch: Any) -> None:
    token, public = _rsa_token()
    monkeypatch.setattr("agent_forge.channels.teams._keys", lambda: _StubJwks(public))
    app = make_app(_cell(TEAMS_APP_ID=TEAMS_APP_ID, TEAMS_GROUP_MAP="u-1:finanzas"))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/channels/teams/messages",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "type": "message",
                    "text": "cual es el limite?",
                    "from": {"aadObjectId": "u-1"},
                    "conversation": {"id": "19:thread"},
                },
            )

    assert response.status_code == 200
    assert ANSWER in response.json()["text"]


async def test_the_teams_webhook_rejects_an_unsigned_activity() -> None:
    app = make_app(_cell(TEAMS_APP_ID=TEAMS_APP_ID))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/channels/teams/messages", json={"type": "message", "text": "hola"}
            )

    assert response.status_code == 401


# ---------------------------------------------------------------------------- Slack


def _sign(
    body: bytes, *, timestamp: str | None = None, secret: str = SLACK_SECRET
) -> dict[str, str]:
    stamp = timestamp or str(int(time.time()))
    basestring = f"v0:{stamp}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return {"x-slack-signature": f"v0={digest}", "x-slack-request-timestamp": stamp}


def test_slack_signature_verification_accepts_a_correctly_signed_request() -> None:
    body = b'{"type":"event_callback"}'

    headers = _sign(body)

    assert verify_slack_signature(
        SLACK_SECRET,
        headers["x-slack-request-timestamp"],
        body,
        headers["x-slack-signature"],
    )


def test_slack_rejects_a_captured_request_replayed_later() -> None:
    """The timestamp window is what makes a stolen request useless after five minutes."""
    body = b'{"type":"event_callback"}'
    stale = str(int(time.time()) - 600)

    headers = _sign(body, timestamp=stale)

    assert not verify_slack_signature(SLACK_SECRET, stale, body, headers["x-slack-signature"])


def test_slack_rejects_a_body_altered_after_signing() -> None:
    headers = _sign(b'{"text":"hola"}')

    assert not verify_slack_signature(
        SLACK_SECRET,
        headers["x-slack-request-timestamp"],
        b'{"text":"borra todo"}',
        headers["x-slack-signature"],
    )


def test_slack_verification_fails_closed_when_no_secret_is_configured() -> None:
    body = b"{}"
    headers = _sign(body, secret="")

    assert not verify_slack_signature(
        "", headers["x-slack-request-timestamp"], body, headers["x-slack-signature"]
    )


def test_a_slack_user_outside_the_group_map_stays_at_c0() -> None:
    event = SlackEvent(type="app_mention", text="hola", user="U-unknown", channel="C1")

    identity = _identity_for(event, _cell(), group_map={"U-1": ["finanzas"]})

    assert identity.groups == ()
    assert identity.classification_ceiling == Classification.C0


async def test_the_slack_webhook_echoes_the_url_verification_challenge() -> None:
    app = make_app(_cell(SLACK_SIGNING_SECRET=SLACK_SECRET))
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/channels/slack/events", content=body, headers=_sign(body)
            )

    assert response.json() == {"challenge": "abc123"}


async def test_the_slack_webhook_rejects_a_wrongly_signed_event() -> None:
    app = make_app(_cell(SLACK_SIGNING_SECRET=SLACK_SECRET))
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/channels/slack/events",
                content=body,
                headers={
                    "x-slack-signature": "v0=deadbeef",
                    "x-slack-request-timestamp": str(int(time.time())),
                },
            )

    assert response.status_code == 401


async def test_the_slack_webhook_ignores_another_bots_message() -> None:
    """Answering a bot is how two bots end up talking to each other forever."""
    app = make_app(_cell(SLACK_SIGNING_SECRET=SLACK_SECRET))
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {"type": "message", "text": "hola", "bot_id": "B1", "channel": "C1"},
        }
    ).encode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/channels/slack/events", content=body, headers=_sign(body)
            )

    assert response.json()["skipped"] == "bot message"


# ------------------------------------------------------------------------ OpenWebUI


async def test_openwebui_connects_with_nothing_but_the_base_url_and_a_key() -> None:
    """The phase's exit criterion for this channel.

    OpenWebUI probes ``{base}/models`` when a connection is added and then posts to
    ``{base}/chat/completions``. Both have to work with no cell-specific configuration,
    because that is all the OpenWebUI connection form collects.
    """
    app = make_app(_cell())
    auth = {"authorization": f"Bearer {API_KEY}"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test/v1") as client:
            listed = await client.get("/models", headers=auth)
            answered = await client.post(
                "/chat/completions",
                headers=auth,
                json={"model": "agent-forge", "messages": [{"role": "user", "content": "hola"}]},
            )

    assert listed.status_code == 200
    assert listed.json()["object"] == "list"
    assert listed.json()["data"], "OpenWebUI hides a connection that lists no model"

    assert answered.status_code == 200
    body = answered.json()
    assert body["choices"][0]["message"]["content"] == ANSWER
    assert body["object"] == "chat.completion"


def test_the_openwebui_pipe_declares_the_valves_an_operator_has_to_fill() -> None:
    """The pipe is copied into OpenWebUI, so its valves are its whole configuration."""
    from agent_forge.channels.openwebui.pipe import Pipe

    pipe = Pipe()

    assert pipe.pipes() == [{"id": "agent-forge", "name": "Agent Forge"}]
    assert set(Pipe.Valves.model_fields) == {
        "AGENT_FORGE_URL",
        "API_KEY",
        "FORWARD_IDENTITY",
        "TIMEOUT",
    }
    assert pipe.valves.API_KEY == "", "a default key would ship a credential in the file"


def test_an_anonymous_channel_identity_sees_only_public_material() -> None:
    """The shared rule behind Teams, Slack and an unmapped OpenWebUI user."""
    identity = Identity(tenant_id="acme-mx", authenticated=False)

    assert identity.is_anonymous
    assert identity.classification_ceiling == Classification.C0
