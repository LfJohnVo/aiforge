"""A real MCP server, used as the far end of the MCP client tests.

Not a mock: an actual MCP server speaking the actual protocol. Runs over stdio or
streamable HTTP so the connector can be exercised on either transport.

    python fake_mcp_server.py                # stdio
    python fake_mcp_server.py --http 8931    # streamable HTTP
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.mcpserver import MCPServer


def build() -> MCPServer:
    server = MCPServer("agent-forge-test")

    @server.tool()
    def echo(message: str) -> str:
        """Return the message, so the caller can prove the round trip."""
        return f"echo: {message}"

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def boom() -> str:
        """Always fail, so the connector's error path is exercised."""
        raise ValueError("deliberate failure")

    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, default=0)
    args = parser.parse_args()

    server = build()
    if args.http:
        server.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=args.http,
            stateless_http=True,
        )
        return 0
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
