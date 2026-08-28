"""MCP surface over NutmegGraph.

Just enough to prove the tool is wired to the graph correctly -- the graph
behavior itself (including get_node) is covered by test_graph.py.
"""

import fakeredis.aioredis as fakeredis
import pytest

import src.mcp_server.server as server
from src.graph import NutmegGraph
from src.status_codes import InvalidQueryError, NodeNotFoundError, StatusCode


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


async def test_get_node_tool_error_carries_the_node_not_found_status_code(graph):
    """MCP tools surface only str(exc) to a masked-error client (see
    fastmcp.server.server's `f"...: {e}"` formatting) -- the [CODE] prefix from
    NutmegError.__str__ is how a client recovers `code` from that plain text."""
    with pytest.raises(NodeNotFoundError) as exc:
        await server.get_node("ghost")

    assert exc.value.code == StatusCode.NODE_NOT_FOUND
    assert str(exc.value).startswith("[NODE_NOT_FOUND]")


async def test_get_node_tool_translates_a_malformed_id_into_invalid_query(graph):
    """Contract (docs/status_codes.md): the status-code contract holds on the MCP
    surface too. graph_writes raises a plain InvalidIdentifierError here; the
    @status_coded decorator is what turns it into a coded error, so a client isn't
    left parsing an uncoded message."""
    with pytest.raises(InvalidQueryError) as exc:
        await server.get_node("bad:id")

    assert exc.value.code == StatusCode.INVALID_QUERY
    assert exc.value.reason == "INVALID_IDENTIFIER"
    assert str(exc.value).startswith("[INVALID_QUERY:INVALID_IDENTIFIER]")


async def test_run_query_tool_error_carries_the_wire_validation_reason(graph):
    """Regression: a malformed plan reaching run_query must keep query_wire's
    specific reason rather than being flattened by the decorator into a generic
    coded error."""
    with pytest.raises(InvalidQueryError) as exc:
        await server.run_query({"wire_version": 99, "start_nodes": ["ada"], "stage_specs": []})

    assert exc.value.reason == "UNSUPPORTED_WIRE_VERSION"


async def test_run_query_tool_executes_a_traversal_plan(graph):
    await graph.add_node("ada", "player", {"name": "Ada"})
    await graph.add_node("celtics", "team")
    await graph.add_edge("ada", "celtics", "plays_for")

    plan = {
        "wire_version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start", "kind": "start"},
            {"name": "teams", "kind": "follow", "sources": ["start"], "edge_type": "plays_for"},
        ],
    }

    result = await server.run_query(plan)

    assert result["stages"]["teams"] == ["celtics"]
