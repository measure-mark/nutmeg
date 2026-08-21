"""Nutmeg MCP server entry point.

    python mcp_main.py

Thin launcher, same pattern as main.py: imports the server object defined in
src/mcp_server/server.py and runs it. Uses streamable-http (not stdio) so the
MCP server is reachable as a network service, matching how the API is served.
"""

import os

from src.mcp_server.server import mcp

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("MCP_PORT", 3888)),
    )
