"""HTTP surface over NutmegGraph.

Swaps the module-level `graph` for one backed by fakeredis so tests never
need a real Redis connection, then exercises each route once to pin its
contract (status code, request/response shape) -- the graph behavior itself
is covered by test_graph.py.

Node ids are plain, globally-unique identifiers with no structure to them --
node_type is already a separate field, so an id like "ada:player" would just
be repeating information the store already has.
"""

import fakeredis
import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.graph import NutmegGraph


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "graph", NutmegGraph(fakeredis.FakeStrictRedis()))
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
    assert client.get("/nodes/ada/degree", params={"edge_type": "plays_for"}).json() == 1


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
        params={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    assert response.status_code == 204
    assert client.get("/nodes/ada/degree").json() == {"total": 0, "by_type": {}}


def test_add_edge_returns_400_if_target_node_does_not_exist(client):
    client.post("/nodes", json={"node_id": "ada", "node_type": "player"})

    response = client.post(
        "/edges",
        json={"source_node": "ada", "target_node": "celtics", "edge_type": "plays_for"},
    )

    assert response.status_code == 400


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
