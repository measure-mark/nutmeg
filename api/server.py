"""FastAPI wrapper around NutmegGraph.

Follows the same wiring convention as nba/mcp_server/server.py: a REDIS_URL
env var (defaulting to localhost) read once at module scope, and the
resulting client handed to the domain class by dependency injection.

Run with: uvicorn api.server:app --reload
"""

import os

import redis
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.graph import NutmegGraph

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL)
graph = NutmegGraph(_redis)

app = FastAPI(title="nutmeg")


@app.exception_handler(ValueError)
def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
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
def add_node(node: NodeCreate) -> None:
    graph.add_node(node.node_id, node.node_type, node.attributes)


@app.delete("/nodes/{node_id}", status_code=204)
def delete_node(node_id: str) -> None:
    graph.delete_node(node_id)


@app.get("/nodes/{node_id}/degree")
def get_degree(node_id: str, edge_type: str | None = None):
    return graph.get_degree(node_id, edge_type)


@app.get("/nodes/{node_id}/neighbors")
def get_neighbors(node_id: str, edge_types: list[str] = Query(default=[])) -> list[str]:
    return graph.get_neighbors(node_id, edge_types)


@app.post("/edges", status_code=204)
def add_edge(edge: EdgeCreate) -> None:
    graph.add_edge(
        edge.source_node, edge.target_node, edge.edge_type, edge.attributes, edge.score
    )


@app.delete("/edges", status_code=204)
def delete_edge(source_node: str, target_node: str, edge_type: str) -> None:
    graph.delete_edge(source_node, target_node, edge_type)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
