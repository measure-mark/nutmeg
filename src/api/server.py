"""FastAPI wrapper around NutmegGraph.

Follows the same wiring convention as nba/mcp_server/server.py: a REDIS_URL
env var (defaulting to localhost) read once at module scope, and the
resulting client handed to the domain class by dependency injection.

This module only defines the app -- run it via the repo-root main.py
(`python main.py`), or with `uvicorn src.api.server:app --reload` for
local autoreload during development.
"""

import os

import redis.asyncio as redis
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.query_engine import QueryExecutor
from src.graph import NutmegGraph

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL)
graph = NutmegGraph(_redis)

app = FastAPI(title="nutmeg")


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """NutmegGraph raises ValueError for a malformed/missing node_id -- that's a bad
    request, not a server error, so it maps to 400 rather than an unhandled 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


class NodeCreate(BaseModel):
    node_id: str
    node_type: str
    attributes: dict = {}


class EdgeCreate(BaseModel):
    source_node: str
    target_node: str
    edge_type: str
    attributes: dict = {}
    score: float = 0


@app.post("/nodes", status_code=204)
async def add_node(node: NodeCreate) -> None:
    await graph.add_node(node.node_id, node.node_type, node.attributes)


@app.delete("/nodes/{node_id}", status_code=204)
async def delete_node(node_id: str) -> None:
    await graph.delete_node(node_id)


@app.get("/nodes/{node_id}")
async def get_node(node_id: str) -> dict:
    return await graph.get_node(node_id)


@app.get("/nodes/{node_id}/degree")
async def get_degree(node_id: str, edge_type: str | None = None):
    return await graph.get_degree(node_id, edge_type)


@app.get("/nodes/{node_id}/neighbors")
async def get_neighbors(
    node_id: str,
    edge_types: list[str] = Query(default=[]),
    start: float | None = None,
    end: float | None = None,
) -> list[str]:
    return await graph.get_neighbors(
        node_id,
        edge_types,
        start=start,
        end=end,
    )


@app.post("/queries/execute")
async def execute_query(query_plan: dict) -> dict:
    return await QueryExecutor(graph).execute(query_plan)


@app.post("/edges", status_code=204)
async def add_edge(edge: EdgeCreate) -> None:
    await graph.add_edge(
        edge.source_node, edge.target_node, edge.edge_type, edge.attributes, edge.score
    )


@app.delete("/edges", status_code=204)
async def delete_edge(source_node: str, target_node: str, edge_type: str) -> None:
    await graph.delete_edge(source_node, target_node, edge_type)
