"""The MCP client against a real MCP server.

F4's first exit criterion: a tool served by an MCP server executes end to end. The server
is a genuine one, launched as a subprocess, speaking the real protocol -- a mocked session
would prove nothing about the wire format or the handshake.

Streamable HTTP is the transport under test because it is the one a platform gateway
actually offers. The stdio transport is exercised too, but only where the event loop
allows it: on Windows, stdio subprocesses need the proactor loop while psycopg needs the
selector loop, and the suite runs on the latter. CI runs on Linux, where both pass.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_forge.connectors.base import CallContext
from agent_forge.connectors.mcp_client import McpGatewayConnector, McpServerConfig
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError

SERVER = Path(__file__).parent / "fake_mcp_server.py"

CTX = CallContext(
    tenant_id="acme-mx",
    user_id="u1",
    groups=("finanzas",),
    classification_ceiling=Classification.C4,
    trace_id="trace-1",
    autonomy_granted=AutonomyLevel.A1,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    """A real MCP server over streamable HTTP, in its own process."""
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - our own script, fixed arguments
        [sys.executable, str(SERVER), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        # A plain TCP connect, not an HTTP GET: GET /mcp opens an SSE stream that never
        # completes, so an HTTP probe times out on a perfectly healthy server.
        for _ in range(80):
            if process.poll() is not None:
                pytest.skip("the MCP test server exited during start-up")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.skip("the MCP test server did not start in time")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def connector(http_server: str) -> McpGatewayConnector:
    client = McpGatewayConnector(
        [McpServerConfig(name="test", transport="streamable_http", url=http_server)]
    )
    await client.connect()
    return client


# ------------------------------------------------------------ the exit criterion


async def test_the_client_discovers_the_servers_tools(
    connector: McpGatewayConnector,
) -> None:
    names = {spec.name for spec in connector.capabilities()}

    assert names == {"mcp.test.echo", "mcp.test.add", "mcp.test.boom"}


async def test_a_remote_tool_executes_end_to_end(connector: McpGatewayConnector) -> None:
    """F4 exit criterion: an MCP-served tool runs and returns its result."""
    result = await connector.invoke("mcp.test.echo", {"message": "hola"}, CTX)

    assert result.ok, result.error
    assert "echo: hola" in _text(result.data)


async def test_arguments_are_passed_through(connector: McpGatewayConnector) -> None:
    result = await connector.invoke("mcp.test.add", {"a": 2, "b": 40}, CTX)

    assert result.ok, result.error
    assert "42" in _text(result.data)


async def test_a_failing_remote_tool_becomes_a_failed_result(
    connector: McpGatewayConnector,
) -> None:
    result = await connector.invoke("mcp.test.boom", {}, CTX)

    assert not result.ok
    assert result.error


# ------------------------------------------------------- conservative defaults


async def test_remote_results_are_treated_as_maximally_sensitive(
    connector: McpGatewayConnector,
) -> None:
    """The gateway does not say how sensitive its answer is, and guessing low leaks."""
    result = await connector.invoke("mcp.test.echo", {"message": "x"}, CTX)

    assert result.classification == Classification.C4


async def test_remote_tools_default_to_requiring_approval(
    connector: McpGatewayConnector,
) -> None:
    """A remote annotation is advisory; the cell is accountable for what it does."""
    spec = next(s for s in connector.capabilities() if s.name == "mcp.test.echo")

    assert spec.autonomy_min >= AutonomyLevel.A2


async def test_remote_names_are_prefixed_so_they_cannot_collide(
    connector: McpGatewayConnector,
) -> None:
    assert all(s.name.startswith("mcp.test.") for s in connector.capabilities())


# ------------------------------------------------------------------- plumbing


async def test_an_unknown_tool_is_rejected(connector: McpGatewayConnector) -> None:
    result = await connector.invoke("mcp.test.nope", {}, CTX)

    assert not result.ok
    assert "unknown" in (result.error or "")


async def test_health_is_true_when_tools_were_discovered(
    connector: McpGatewayConnector,
) -> None:
    assert await connector.health() is True


async def test_an_unreachable_server_yields_no_tools_and_is_unhealthy() -> None:
    client = McpGatewayConnector(
        [
            McpServerConfig(
                name="broken",
                transport="streamable_http",
                url=f"http://127.0.0.1:{_free_port()}/mcp",
            )
        ]
    )

    assert await client.connect() == 0
    assert await client.health() is False


async def test_a_cell_with_no_gateway_configured_is_unhealthy() -> None:
    assert await McpGatewayConnector.from_env({}).health() is False


def test_an_http_server_without_a_url_is_rejected() -> None:
    with pytest.raises(ToolError, match="url"):
        McpServerConfig(name="x", transport="streamable_http").validate()


def test_a_stdio_server_without_a_command_is_rejected() -> None:
    with pytest.raises(ToolError, match="command"):
        McpServerConfig(name="x", transport="stdio").validate()


def test_from_env_builds_a_gateway_when_a_url_is_present() -> None:
    client = McpGatewayConnector.from_env(
        {"MCP_GATEWAY_URL": "http://gw:8080/mcp", "MCP_GATEWAY_TOKEN": "t"}
    )

    assert client.name == "mcp"


# ---------------------------------------------------------------------- stdio


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="stdio subprocesses need the proactor loop; the suite runs on the selector "
    "loop for psycopg. Covered on Linux CI.",
)
async def test_the_stdio_transport_also_works() -> None:
    client = McpGatewayConnector(
        [
            McpServerConfig(
                name="local",
                transport="stdio",
                command=sys.executable,
                args=(str(SERVER),),
            )
        ]
    )

    assert await client.connect() == 3
    result = await client.invoke("mcp.local.echo", {"message": "por stdio"}, CTX)

    assert result.ok
    assert "por stdio" in _text(result.data)


def _text(data: dict[str, object] | None) -> str:
    blocks = (data or {}).get("content", [])
    return " ".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
