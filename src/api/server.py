"""FastAPI wrapper around NutmegGraph.

Follows the same wiring convention as nba/mcp_server/server.py: a REDIS_URL
env var (defaulting to localhost) read once at module scope, and the
resulting client handed to the domain class by dependency injection.

This module only defines the app -- run it via the repo-root main.py
(`python main.py`), or with `uvicorn src.api.server:app --reload` for
local autoreload during development.
"""

import os

# Not `import redis.asyncio as redis`: the `import redis.exceptions` below rebinds
# the bare name `redis` to the top-level (synchronous) package, which would silently
# turn from_url into the sync client and make every awaited command fail.
import redis.asyncio
import redis.exceptions
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.query_engine import QueryExecutor
from src.bulk_redis import BulkRedisLoader, DEFAULT_BATCH_SIZE
from src.error_adapter import as_nutmeg_error
from src.graph import NutmegGraph
from src.status_codes import InvalidQueryError
from src.telemetry import setup_telemetry

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.asyncio.from_url(REDIS_URL)
graph = NutmegGraph(_redis)
bulk_loader = BulkRedisLoader(_redis)

app = FastAPI(title="nutmeg")

# Here rather than in main.py so `uvicorn src.api.server:app --reload` is traced too.
# No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set -- see src/telemetry.py.
setup_telemetry("nutmeg-api", app)


# The handlers below are this API's half of the Adapter layer: graph.py,
# graph_writes.py, and bulk_redis.py deliberately raise plain Python exceptions (see
# graph_writes.py's module docstring) so they stay usable outside an HTTP context.
# Which status code each of those exceptions carries is decided once, in
# src.error_adapter, shared with the MCP server; all that's left here is turning
# the resulting NutmegError into an HTTP response -- Nutmeg is a client/server
# protocol with its own `code`/`reason` contract, not a REST resource API, so HTTP
# status is secondary transport metadata layered on top of exc.to_dict(), not the
# contract itself.
#
# Two handlers cover every case because Starlette dispatches on the first match in
# the exception's MRO: NutmegError and the graph layer's errors are all ValueErrors,
# and a lost graph store is a RedisError.


@app.exception_handler(ValueError)
@app.exception_handler(redis.exceptions.RedisError)
async def nutmeg_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Every error client code should switch on -- see docs/status_codes.md. Body
    shape is exc.to_dict(): code, detail, and reason when the code carries one."""
    err = as_nutmeg_error(exc)
    return JSONResponse(status_code=err.http_status, content=err.to_dict())


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A request body FastAPI/Pydantic rejects before a route even runs (missing or
    wrongly typed fields) -- still carries `code`/`reason`, per this module's
    docstring, rather than falling back to FastAPI's default unstructured 422 body."""
    err = InvalidQueryError(
        f"request body failed validation: {exc.errors()}", reason="INVALID_REQUEST_DOCUMENT"
    )
    return JSONResponse(status_code=err.http_status, content=err.to_dict())


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


# node_id is a query parameter, not a path segment. Node ids are opaque strings
# supplied by the caller, and a '/' in one cannot survive a path segment: ASGI
# servers percent-decode before routing, so /nodes/a%2Fb arrives as two segments
# and matches no route. A node stored under such an id would be permanently
# unreadable. As a query parameter it round-trips like any other value, and no
# character has to be barred from an id to keep the API addressable.
@app.delete("/nodes", status_code=204)
async def delete_node(node_id: str) -> None:
    await graph.delete_node(node_id)


@app.get("/nodes")
async def get_node(node_id: str) -> dict:
    return await graph.get_node(node_id)


@app.get("/nodes/degree")
async def get_degree(node_id: str, edge_type: str | None = None):
    return await graph.get_degree(node_id, edge_type)


@app.get("/nodes/neighbors")
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
