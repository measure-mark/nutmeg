"""MCP server exposing NutmegGraph to model clients.

Follows the same wiring convention as src/api/server.py: a REDIS_URL env var
(defaulting to localhost) read once at module scope, and the resulting client
handed to NutmegGraph by dependency injection.

This module only defines the server; run it via the repo-root mcp_main.py
(`python mcp_main.py`).
"""

import os

import redis.asyncio as redis
from fastmcp import FastMCP

from src.api.query_engine import QueryExecutor
from src.error_adapter import status_coded
from src.graph import NutmegGraph
import numpy as np

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL)
graph = NutmegGraph(_redis)

mcp = FastMCP("nutmeg", instructions="Read access to the Nutmeg graph.")


# Every tool is wrapped in @status_coded: an MCP client sees only str(exc), so the
# `[CODE]` (or `[CODE:REASON]`) prefix a NutmegError prints is the whole of the
# status-code contract on this surface. Without it a graph-layer error (a malformed
# node_id, say) would reach the client as uncoded text. See docs/status_codes.md.


@mcp.tool()
@status_coded
async def get_node(node_id: str) -> dict:
    """A node's type, attributes, and out-degree.

    node_id: the node to look up. Raises if it hasn't been added.
    """
    return await graph.get_node(node_id)


@mcp.tool()
@status_coded
async def get_meta_graph() -> dict:
    """Return live node-type and typed-edge counts for planning graph traversals."""
    return await graph.get_meta_graph()

@mcp.tool()
@status_coded
async def run_query(query_plan: dict) -> dict:
    """Execute a graph query plan and return stage results and requested metadata.

        The query_plan is a JSON object using wire_version 1:

        {
            "wire_version": 1,
            "start_nodes": ["ada"],
            "stage_specs": [
                {"name": "start", "kind": "start"},
                {"name": "teams", "kind": "follow", "sources": ["start"],
                 "edge_type": "plays_for", "attributes": true, "scores": true}
            ]
        }

        Query rules:
        - start_nodes is a non-empty list of node ids. Duplicate ids are removed.
        - stage_specs is a non-empty list with exactly one start stage. Each stage
            has a unique name and may refer only to other stage names in sources.
        - A start stage has kind "start" and no sources. It yields start_nodes.
        - A follow stage has kind "follow", exactly one source, and an edge_type.
            It follows outgoing edges of that type. Optional numeric start and end
            are inclusive score bounds. Optional degrees, attributes, and scores
            request metadata in the response.
        - A set stage has kind "union", "intersect", "subtract", or
            "symmetric_difference", and exactly two sources. Set stages may request
            degrees or attributes, but not scores.
        - Stage dependencies must be acyclic. Stages execute in dependency order.

        Metadata rules:
        - Set "attributes": true to include each returned node's stored attributes.
        - Set "degrees": true to include each returned node's out-degree as
            {"total": number, "by_type": {"edge_type": number}}.
        - Metadata is returned once per node in nodes, even when several stages
            request it. A stage's node ids remain in stages; nodes is not stage-scoped.
        - Set "scores": true only on a follow stage to include its edge scores in
            scores under that stage name.

        Use get_meta_graph to discover valid node and edge types. The response has
        wire_version, stages (stage name to node-id list), nodes (requested
        metadata), and scores (only for stages requesting scores).
    """
    return await QueryExecutor(graph).execute(query_plan)
