"""HTTP surface over NutmegGraph.

Swaps the module-level `graph` for one backed by fakeredis so tests never
need a real Redis connection, then exercises each route once to pin its
contract (status code, request/response shape) -- the graph behavior itself
is covered by test_graph.py.
"""

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api.server as server
from src.graph import NutmegGraph


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "graph", NutmegGraph(fakeredis.FakeStrictRedis()))
    return TestClient(server.app)


def test_add_node_then_add_edge_then_degree(client):
    client.post("/nodes", json={"node_id": "player:1", "node_type": "player"})
    client.post("/nodes", json={"node_id": "team:BOS", "node_type": "team"})
    response = client.post(
        "/edges",
        json={"source_node": "player:1", "target_node": "team:BOS", "edge_type": "plays_for"},
    )

    assert response.status_code == 204
    assert client.get("/nodes/player:1/degree").json() == {
        "total": 1,
        "by_type": {"plays_for": 1},
    }
    assert client.get("/nodes/player:1/degree", params={"edge_type": "plays_for"}).json() == 1


def test_get_neighbors_filters_by_edge_type(client):
    client.post(
        "/edges",
        json={"source_node": "player:1", "target_node": "team:BOS", "edge_type": "plays_for"},
    )
    client.post(
        "/edges",
        json={"source_node": "player:1", "target_node": "player:2", "edge_type": "teammate_of"},
    )

    response = client.get("/nodes/player:1/neighbors", params={"edge_types": ["plays_for"]})

    assert response.json() == ["team:BOS"]


def test_delete_edge_then_degree_drops_to_zero(client):
    client.post(
        "/edges",
        json={"source_node": "player:1", "target_node": "team:BOS", "edge_type": "plays_for"},
    )

    response = client.request(
        "DELETE",
        "/edges",
        params={"source_node": "player:1", "target_node": "team:BOS", "edge_type": "plays_for"},
    )

    assert response.status_code == 204
    assert client.get("/nodes/player:1/degree").json() == {"total": 0, "by_type": {}}


def test_delete_node_cascades_through_the_api(client):
    """Same cascade contract as test_graph.py's version, exercised through HTTP."""
    client.post("/nodes", json={"node_id": "player:1", "node_type": "player"})
    client.post("/nodes", json={"node_id": "team:BOS", "node_type": "team"})
    client.post(
        "/edges",
        json={"source_node": "player:1", "target_node": "team:BOS", "edge_type": "plays_for"},
    )

    response = client.delete("/nodes/team:BOS")

    assert response.status_code == 204
    assert client.get("/nodes/player:1/neighbors").json() == []
