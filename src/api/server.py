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
import redis.exceptions
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.query_engine import QueryExecutor
from src.bulk_redis import BulkRedisLoader, DEFAULT_BATCH_SIZE
from src.graph import NutmegGraph
from src.status_codes import NutmegError, ServiceUnavailableError

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL)
graph = NutmegGraph(_redis)
bulk_loader = BulkRedisLoader(_redis)

app = FastAPI(title="nutmeg")


@app.exception_handler(NutmegError)
async def nutmeg_error_handler(request: Request, exc: NutmegError) -> JSONResponse:
    """Every NutmegGraph/QueryExecutor error client code should switch on -- see
    README.md's Status Codes section. Body shape is exc.to_dict(): code, detail,
    and reason when the code carries one."""
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(redis.exceptions.RedisError)
async def redis_error_handler(request: Request, exc: redis.exceptions.RedisError) -> JSONResponse:
    """A lost connection or timed-out call to the graph store -- the request may
    have never reached Redis, so it maps to SERVICE_UNAVAILABLE rather than a
    generic 500, telling the client it's safe to retry."""
    err = ServiceUnavailableError(f"redis error: {exc}")
    return JSONResponse(status_code=err.http_status, content=err.to_dict())


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Fallback for a plain ValueError not carrying a status code (e.g. NutmegGraph's
    node-type-conflict error) -- still a bad request, not a server error, so it maps
    to 400 rather than an unhandled 500."""
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


class BulkLoadRequest(BaseModel):
    nodes: list[NodeCreate] = Field(default_factory=list)
    edges: list[EdgeCreate] = Field(default_factory=list)
    batch_size: int = DEFAULT_BATCH_SIZE


@app.post("/nodes", status_code=204)
async def add_node(node: NodeCreate) -> None:
    await graph.add_node(node.node_id, node.node_type, node.attributes)


@app.post("/bulk-load")
async def bulk_load(request: BulkLoadRequest) -> dict:
    return await bulk_loader.load(
        (node.model_dump() for node in request.nodes),
        (edge.model_dump() for edge in request.edges),
        batch_size=request.batch_size,
    )


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


@app.get("/meta")
async def get_meta_graph() -> dict:
    return await graph.get_meta_graph()


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
