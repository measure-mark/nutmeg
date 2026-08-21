"""MCP surface over NutmegGraph.

Just enough to prove the tool is wired to the graph correctly -- the graph
behavior itself (including get_node) is covered by test_graph.py.
"""

import fakeredis
import pytest

import src.mcp_server.server as server
from src.graph import NutmegGraph


@pytest.fixture
def graph(monkeypatch):
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    monkeypatch.setattr(server, "graph", g)
    return g


def test_get_node_tool_returns_type_attributes_and_degree(graph):
    graph.add_node("ada", "player", {"name": "Ada"})
    graph.add_node("celtics", "team")
    graph.add_edge("ada", "celtics", "plays_for")

    assert server.get_node("ada") == {
        "node_type": "player",
        "attributes": {"name": "Ada"},
        "degree": {"total": 1, "by_type": {"plays_for": 1}},
    }


def test_get_node_tool_raises_if_node_does_not_exist(graph):
    with pytest.raises(ValueError):
        server.get_node("ghost")
