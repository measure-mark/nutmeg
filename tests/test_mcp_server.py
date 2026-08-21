"""MCP surface over NutmegGraph.

Just enough to prove the tool is wired to the graph correctly -- the graph
behavior itself (including get_node) is covered by test_graph.py.
"""

import fakeredis.aioredis as fakeredis
import pytest

import src.mcp_server.server as server
from src.graph import NutmegGraph


@pytest.fixture
def graph(monkeypatch):
    g = NutmegGraph(fakeredis.FakeRedis())
    monkeypatch.setattr(server, "graph", g)
    return g


async def test_get_node_tool_returns_type_attributes_and_degree(graph):
    await graph.add_node("ada", "player", {"name": "Ada"})
    await graph.add_node("celtics", "team")
    await graph.add_edge("ada", "celtics", "plays_for")

    assert await server.get_node("ada") == {
        "node_type": "player",
        "attributes": {"name": "Ada"},
        "degree": {"total": 1, "by_type": {"plays_for": 1}},
    }


async def test_get_node_tool_raises_if_node_does_not_exist(graph):
    with pytest.raises(ValueError):
        await server.get_node("ghost")
