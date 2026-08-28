"""HTTP surface over NutmegGraph.

Swaps the module-level `graph` for one backed by fakeredis so tests never
need a real Redis connection, then exercises each route once to pin its
contract (status code, request/response shape) -- the graph behavior itself
is covered by test_graph.py.

Node ids are plain, globally-unique identifiers with no structure to them --
node_type is already a separate field, so an id like "ada:player" would just
be repeating information the store already has.
"""

import fakeredis.aioredis as fakeredis
import pytest
import redis.exceptions
from fastapi.testclient import TestClient

import src.api.server as server
from src.bulk_redis import BulkLoadInterruptedError, BulkRedisLoader
from src.graph import NutmegGraph


@pytest.fixture
def client(monkeypatch):
    redis_client = fakeredis.FakeRedis()
    monkeypatch.setattr(server, "graph", NutmegGraph(redis_client))
    monkeypatch.setattr(server, "bulk_loader", BulkRedisLoader(redis_client))
    return TestClient(server.app)


def test_add_node_then_add_edge_then_degree(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})
    client.post("/nodes", json={"node_id": "celtics", "node_type": "team"})
    response = client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    assert response.status_code == 204
    assert client.get("/nodes/ada/degree").json() == {
        "total": 1,
        "by_type": {"plays_for": 1},
    }
    assert (
        client.get("/nodes/ada/degree", params={"edge_type": "plays_for"}).json() == 1
    )


def test_bulk_load_route_pipelines_nodes_before_edges(client):
    response = client.post(
        "/bulk-load",
        json={
            "nodes": [
                {"node_id": "ada", "node_type": "player"},
                {"node_id": "celtics", "node_type": "team"},
            ],
            "edges": [
                {
                    "source_node": "ada",
                    "target_node": "celtics",
                    "edge_type": "plays_for",
                }
            ],
            "batch_size": 1,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "nodes_loaded": 2,
        "edges_loaded": 1,
        "errors": [],
        "code": "OK",
    }
    assert client.get("/nodes/ada/neighbors").json() == ["celtics"]


def test_bulk_load_route_uses_loader_batch_size_validation(client):
    response = client.post("/bulk-load", json={"batch_size": 0})

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_QUERY",
        "detail": "batch_size 0 is outside the allowed range of 1 to 10000",
        "reason": "INVALID_BATCH_SIZE",
    }


def test_interrupted_bulk_load_maps_to_service_unavailable(client, monkeypatch):
    """Contract (docs/BULK_LOAD.md): a Redis failure mid-load is not a
    BULK_PARTIAL_FAILURE body -- it's a SERVICE_UNAVAILABLE error whose detail
    carries the counts that landed before the interruption, so a caller knows
    the load stopped and where a retry can safely resume."""

    async def interrupted_load(nodes, edges, *, batch_size):
        raise BulkLoadInterruptedError(
            redis.exceptions.ConnectionError("connection refused"), 7, 0, []
        )

    monkeypatch.setattr(server.bulk_loader, "load", interrupted_load)

    response = client.post("/bulk-load", json={"nodes": [], "edges": []})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert "nodes_loaded=7" in body["detail"]


def test_get_node_returns_node_document(client):
    client.post(
        "/nodes",
        json={"node_id": "ada", "node_type": "player", "attributes": {"name": "Ada"}},
    )

    assert client.get("/nodes/ada").json() == {
        "node_type": "player",
        "attributes": {"name": "Ada"},
        "degree": {"total": 0, "by_type": {}},
    }


def test_get_node_returns_node_not_found_status_code_if_missing(client):
    response = client.get("/nodes/ghost")

    assert response.status_code == 404
    assert response.json() == {"code": "NODE_NOT_FOUND", "detail": "node 'ghost' does not exist"}


def test_node_type_conflict_maps_to_data_error(client):
    """Contract: re-adding a node under a different (immutable) type is a
    conflict with existing data, not a malformed request -- DATA_ERROR/409,
    not INVALID_QUERY/400."""
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})

    response = client.post("/nodes", json={"node_id": "ada", "node_type": "team"})

    assert response.status_code == 409
    assert response.json() == {
        "code": "DATA_ERROR",
        "detail": "node 'ada' already has type 'player'",
        "reason": "NODE_TYPE_CONFLICT",
    }


def test_invalid_node_id_maps_to_invalid_query(client):
    """Contract: a malformed node_id is still INVALID_QUERY/400, even though
    graph.py itself (see graph_writes.InvalidIdentifierError) raises a plain
    ValueError with no status code -- the translation happens at this layer."""
    response = client.post("/nodes", json={"node_id": "bad:id", "node_type": "player"})

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_QUERY",
        "detail": "Invalid node_id: 'bad:id'",
        "reason": "INVALID_IDENTIFIER",
    }


def test_missing_required_field_maps_to_invalid_query(client):
    """Contract: a request FastAPI/Pydantic rejects before the route runs (here,
    a missing node_type) still carries `code`/`reason`, not FastAPI's default
    unstructured 422 body."""
    response = client.post("/nodes", json={"node_id": "ada"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_QUERY"
    assert body["reason"] == "INVALID_REQUEST_DOCUMENT"
    assert "node_type" in body["detail"]


def test_redis_connection_error_maps_to_service_unavailable(client, monkeypatch):
    """Contract: a lost/unreachable Redis connection is reported as
    SERVICE_UNAVAILABLE, not an unhandled 500, so clients know to retry."""

    async def broken_get_node(node_id):
        raise redis.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(server.graph, "get_node", broken_get_node)

    response = client.get("/nodes/ada")

    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def test_get_neighbors_filters_by_edge_type(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})
    client.post("/nodes", json={"node_id": "celtics", "node_type": "team"})
    client.post("/nodes", json={"node_id": "grace", "node_type": "player"})
    client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )
    client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "grace", "edge_type": "teammate_of"},
    )

    response = client.get("/nodes/ada/neighbors", params={"edge_types": ["plays_for"]})

    assert response.json() == ["celtics"]


def test_get_neighbors_accepts_score_window(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})
    for node_id, score in [("heat", 2005), ("lakers", 2010), ("cavaliers", 2015)]:
        client.post("/nodes", json={"node_id": node_id, "node_type": "team"})
        client.post(
            "/edges",
            json={
                "source_node": "ada",
                "target_node": node_id,
                "edge_type": "played_for",
                "score": score,
            },
        )

    response = client.get(
        "/nodes/ada/neighbors",
        params={"edge_types": ["played_for"], "start": 2010, "end": 2015},
    )

    assert response.json() == ["lakers", "cavaliers"]


def test_execute_query_runs_server_side(client):
    for node_id in ["ada", "bob"]:
        client.post("/nodes", json={"node_id": node_id, "node_type": "person"})
    client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "bob", "edge_type": "connected_to"},
    )

    response = client.post(
        "/queries/execute",
        json={
            "wire_version": 1,
            "start_nodes": ["ada"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "connected",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                    "attributes": True,
                    "scores": True,
                },
            ],
        },
    )

    assert response.json() == {
        "wire_version": 1,
        "stages": {"start_stage": ["ada"], "connected": ["bob"]},
        "nodes": {"bob": {"node_type": "person", "attributes": {}}},
        "scores": {"connected": {"bob": 0.0}},
    }


def test_execute_query_over_max_depth_returns_max_depth_exceeded(client):
    """End-to-end check that query_wire's MAX_QUERY_DEPTH is actually enforced
    through the real HTTP route, not just in the unit-level query_wire tests."""
    from src.query_wire import MAX_QUERY_DEPTH

    stages = [{"name": "start_stage", "kind": "start"}]
    previous = "start_stage"
    for i in range(MAX_QUERY_DEPTH + 1):
        name = f"stage{i}"
        stages.append(
            {"name": name, "kind": "follow", "sources": [previous], "edge_type": "connected_to"}
        )
        previous = name

    response = client.post(
        "/queries/execute",
        json={"wire_version": 1, "start_nodes": ["ada"], "stage_specs": stages},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "MAX_DEPTH_EXCEEDED"


def test_delete_edge_then_degree_drops_to_zero(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})
    client.post("/nodes", json={"node_id": "celtics", "node_type": "team"})
    client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    response = client.request(
        "DELETE",
        "/edges",
        params={
            "source_node": "ada",
            "target_node": "celtics",
            "edge_type": "plays_for",
        },
    )

    assert response.status_code == 204
    assert client.get("/nodes/ada/degree").json() == {"total": 0, "by_type": {}}


def test_add_edge_returns_404_if_target_node_does_not_exist(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})

    response = client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "NODE_NOT_FOUND",
        "detail": "target node 'celtics' does not exist",
    }


def test_delete_node_cascades_through_the_api(client):
    """Same cascade contract as test_graph.py's version, exercised through HTTP."""
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})
    client.post("/nodes", json={"node_id": "celtics", "node_type": "team"})
    client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    response = client.delete("/nodes/celtics")

    assert response.status_code == 204
    assert client.get("/nodes/ada/neighbors").json() == []
