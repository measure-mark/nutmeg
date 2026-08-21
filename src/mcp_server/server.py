"""MCP server exposing NutmegGraph to model clients.

Follows the same wiring convention as src/api/server.py: a REDIS_URL env var
(defaulting to localhost) read once at module scope, and the resulting client
handed to NutmegGraph by dependency injection.

Just a stub for now -- get_node() is enough to prove the MCP infrastructure
(container, port, transport) works end to end. More tools land here as the
graph grows use cases that actually need them (CLAUDE.md: no abstraction, or
in this case no tool, ahead of a real need for it).

This module only defines the server; run it via the repo-root mcp_main.py
(`python mcp_main.py`).
"""

import os

import redis.asyncio as redis
from fastmcp import FastMCP

from src.graph import NutmegGraph

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL)
graph = NutmegGraph(_redis)

mcp = FastMCP("nutmeg", instructions="Read access to the Nutmeg graph.")


@mcp.tool()
async def get_node(node_id: str) -> dict:
    """A node's type, attributes, and out-degree.

    node_id: the node to look up. Raises if it hasn't been added.
    """
    return await graph.get_node(node_id)
