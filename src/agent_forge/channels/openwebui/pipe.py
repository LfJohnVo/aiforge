"""OpenWebUI pipe for an Agent Forge cell.

Copy this file into OpenWebUI (*Workspace -> Functions -> New Function*) when you want the
cell's citations and approval states rendered as first-class UI, rather than buried in the
answer text.

**You usually do not need it.** The cell already speaks the OpenAI protocol, so adding it
under *Settings -> Connections -> OpenAI API* with the base URL and an API key works with
no code at all. This pipe exists for two things that connection cannot do: forward the
OpenWebUI user's identity so the cell can apply identity-aware retrieval, and surface
citations and `awaiting_approval` distinctly.

This module is **not** imported by the cell. It runs inside OpenWebUI.
"""

from __future__ import annotations

from typing import Any

import requests  # provided by the OpenWebUI runtime
from pydantic import BaseModel, Field


class Pipe:
    """OpenWebUI pipe function."""

    class Valves(BaseModel):
        AGENT_FORGE_URL: str = Field(
            default="http://agent-api:8080",
            description="Base URL of the Agent Forge cell",
        )
        API_KEY: str = Field(default="", description="Tenant API key")
        FORWARD_IDENTITY: bool = Field(
            default=True,
            description=(
                "Forward the OpenWebUI user's id and groups. Without this the cell sees "
                "an anonymous requester and answers only from public (C0) material."
            ),
        )
        TIMEOUT: int = Field(default=120, description="Seconds to wait for an answer")

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "agent_forge"
        self.name = "Agent Forge"
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "agent-forge", "name": "Agent Forge"}]

    def pipe(self, body: dict[str, Any], __user__: dict[str, Any] | None = None) -> str:
        """Send one turn to the cell and render its answer."""
        headers = {"content-type": "application/json"}
        if self.valves.API_KEY:
            headers["authorization"] = f"Bearer {self.valves.API_KEY}"

        payload: dict[str, Any] = {
            "model": "agent-forge",
            "messages": body.get("messages", []),
            "stream": False,
        }
        if self.valves.FORWARD_IDENTITY and __user__:
            # The cell reads identity from a verified token; these headers are for a
            # deployment where OpenWebUI sits behind the same trust boundary. In a
            # zero-trust setup, configure OIDC on the cell instead.
            headers["x-forwarded-user"] = str(__user__.get("id", ""))
            headers["x-forwarded-groups"] = ",".join(__user__.get("groups", []) or [])

        try:
            response = requests.post(
                f"{self.valves.AGENT_FORGE_URL.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.valves.TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            return f"No pude contactar con la celula: {exc}"

        answer = data["choices"][0]["message"]["content"]
        citations = data.get("x_citations") or []
        if citations:
            answer += "\n\n---\n**Fuentes**\n" + "\n".join(f"- `{c}`" for c in citations)
        if data.get("x_status") == "awaiting_approval":
            answer += (
                "\n\n> ⏸️ Esta accion requiere aprobacion humana. "
                f"Tarea `{data.get('x_task_id', '')}`."
            )
        return str(answer)
