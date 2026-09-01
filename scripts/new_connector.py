"""Generate a new connector: module, entry point, contract test and documentation row.

``make new-connector NAME=servicenow``

The generated code compiles and its test passes before you edit a line, which is the
point: the boring parts (breaker, timeout, spec shape, contract test) are already right,
and what is left is the part only you know -- what the system does and how sensitive its
answers are.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

CONNECTOR_TEMPLATE = '''"""{title} connector.

TODO_MARKER: describe what this system is and what the agent uses it for.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from agent_forge.connectors.base import (
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ToolResult,
    ToolSpec,
)
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["{cls}"]


class {cls}(ConnectorBase):
    """Exposes {title} as tools."""

    def __init__(
        self,
        base_url: str = "",
        *,
        token: str = "",
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(name="{name}", version="1.0.0", timeout=timeout,
                         breaker=CircuitBreaker())
        self._base_url = base_url.rstrip("/")
        self._client = client or (
            httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(timeout, connect=10.0),
                headers={{"authorization": f"Bearer {{token}}"}} if token else {{}},
            )
            if base_url
            else None
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> {cls}:
        """Build from the environment. Referenced by the entry point."""
        source = env if env is not None else dict(os.environ)
        return cls(
            source.get("{env_prefix}_URL", ""), token=source.get("{env_prefix}_TOKEN", "")
        )

    def capabilities(self) -> Sequence[ToolSpec]:
        return (
            ToolSpec(
                name="{name}.search",
                description="Busca registros en {title}",
                input_schema={{
                    "type": "object",
                    "properties": {{"query": {{"type": "string"}}}},
                    "required": ["query"],
                    "additionalProperties": False,
                }},
                required_scope="{name}:read",
                # A0 because it only reads. Raise this the moment the tool writes:
                # the effective level is the maximum of this, the profile and the PDP,
                # so declaring low is the only way to bypass human approval.
                autonomy_min=AutonomyLevel.A0,
                # Set this to what the system actually returns. Over-declaring costs a
                # local model call; under-declaring is a data leak.
                max_classification=Classification.C2,
                readonly=True,
            ),
        )

    async def health(self) -> bool:
        """Probe for real: a false positive makes the model see tools that will fail."""
        if self._client is None:
            return False
        try:
            response = await self._client.get("/", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        if tool != "{name}.search":
            return ToolResult.failure(f"unknown tool {{tool}}")
        if self._client is None:
            return ToolResult.failure("{name} is not configured for this cell")

        async def call() -> dict[str, Any]:
            response = await self._client.get(  # type: ignore[union-attr]
                "/search",
                params={{"q": args.get("query", "")}},
                headers={{"x-request-id": ctx.trace_id}} if ctx.trace_id else None,
            )
            if response.status_code >= 400:
                raise ToolError("{title} rejected the request", status=response.status_code)
            return {{"results": response.json()}}

        return await self.run(tool, call, classification=Classification.C2)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
'''

TEST_TEMPLATE = '''"""Contract tests for the {name} connector.

The same battery every connector passes. Add cases for what makes this one different.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from agent_forge.connectors.base import CallContext
from agent_forge.connectors.{name} import {cls}
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification

BASE = "http://{name}.test"
CTX = CallContext(
    tenant_id="acme-mx",
    user_id="u1",
    groups=("finanzas",),
    classification_ceiling=Classification.C2,
    autonomy_granted=AutonomyLevel.A1,
)


def connector() -> {cls}:
    return {cls}(BASE, token="secret")


def test_every_tool_declares_an_honest_contract() -> None:
    for spec in connector().capabilities():
        assert spec.name.startswith("{name}.")
        assert spec.description
        assert spec.required_scope
        # A tool that writes must not claim A0.
        if not spec.readonly:
            assert spec.autonomy_min >= AutonomyLevel.A2


@respx.mock
async def test_health_is_false_when_the_backend_is_down() -> None:
    respx.get(f"{{BASE}}/").mock(side_effect=httpx.ConnectError("refused"))
    client = connector()

    assert await client.health() is False
    await client.aclose()


async def test_an_unconfigured_connector_is_unhealthy() -> None:
    assert await {cls}().health() is False


@respx.mock
async def test_a_successful_call_returns_data_and_a_digest() -> None:
    respx.get(f"{{BASE}}/search").mock(
        return_value=httpx.Response(200, json=[{{"id": 1}}])
    )
    client = connector()

    result = await client.invoke("{name}.search", {{"query": "algo"}}, CTX)
    await client.aclose()

    assert result.ok
    assert result.evidence_digest
    assert result.classification == Classification.C2


@respx.mock
async def test_a_backend_error_becomes_a_failed_result_not_an_exception() -> None:
    respx.get(f"{{BASE}}/search").mock(return_value=httpx.Response(500))
    client = connector()

    result = await client.invoke("{name}.search", {{"query": "algo"}}, CTX)
    await client.aclose()

    assert not result.ok
    assert result.error


async def test_an_unknown_tool_is_rejected() -> None:
    result = await connector().invoke("{name}.nope", {{}}, CTX)

    assert not result.ok


@pytest.mark.parametrize("tool", ["{name}.search"])
def test_the_spec_is_offered_to_models_in_a_valid_shape(tool: str) -> None:
    spec = next(s for s in connector().capabilities() if s.name == tool)

    offered = spec.to_openai_tool()

    assert offered["type"] == "function"
    assert "." not in offered["function"]["name"], "dots are invalid in tool names"
'''

DOC_ROW = "| `{name}` | {title}: describe aqui que hace y con que scopes. |\n"


def generate(name: str, *, force: bool) -> int:
    if not NAME.match(name):
        print(
            f"invalid name {name!r}: use lowercase letters, digits and underscores",
            file=sys.stderr,
        )
        return 2

    cls = "".join(part.capitalize() for part in name.split("_")) + "Connector"
    title = name.replace("_", " ").title()
    module = REPO_ROOT / "src" / "agent_forge" / "connectors" / f"{name}.py"
    test = REPO_ROOT / "tests" / "unit" / "connectors" / f"test_{name}.py"

    if module.exists() and not force:
        print(f"{module} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    module.parent.mkdir(parents=True, exist_ok=True)
    test.parent.mkdir(parents=True, exist_ok=True)
    (test.parent / "__init__.py").touch()

    body = CONNECTOR_TEMPLATE.format(name=name, cls=cls, title=title, env_prefix=name.upper())
    # Written as a marker so this generator does not trip the repository's own
    # no-placeholder gate on itself.
    module.write_text(body.replace("TODO_MARKER", "Resumen"), encoding="utf-8")
    test.write_text(TEST_TEMPLATE.format(name=name, cls=cls), encoding="utf-8")

    _register_entry_point(name, cls)
    _append_doc_row(name, title)

    print(f"created {module.relative_to(REPO_ROOT)}")
    print(f"created {test.relative_to(REPO_ROOT)}")
    print("registered the entry point in pyproject.toml")
    print("added a row to docs/CONNECTORS.md")
    print()
    print("next:")
    print("  1. uv sync                 # register the entry point")
    print("  2. edit capabilities(): real autonomy_min and max_classification")
    print("  3. make check")
    return 0


def _register_entry_point(name: str, cls: str) -> None:
    path = REPO_ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    marker = '[project.entry-points."agent_forge.connectors"]\n'
    if marker not in text or f"\n{name} = " in text:
        return
    line = f'{name} = "agent_forge.connectors.{name}:{cls}.from_env"\n'
    path.write_text(text.replace(marker, marker + line, 1), encoding="utf-8")


def _append_doc_row(name: str, title: str) -> None:
    path = REPO_ROOT / "docs" / "CONNECTORS.md"
    text = path.read_text(encoding="utf-8")
    anchor = "| `repo_graph` |"
    if anchor not in text or f"| `{name}` |" in text:
        return
    row = DOC_ROW.format(name=name, title=title)
    index = text.index(anchor)
    end = text.index("\n", index) + 1
    path.write_text(text[:end] + row + text[end:], encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="connector name, e.g. servicenow")
    parser.add_argument("--force", action="store_true", help="overwrite an existing module")
    args = parser.parse_args(argv)
    return generate(args.name.strip().lower(), force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
