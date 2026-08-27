"""Server-side query execution over NutmegGraph."""

import asyncio

import fakeredis.aioredis as fakeredis
import pytest

from src.api.query_engine import QueryExecutor
from src.client import NutmegClient
from src.graph import NutmegGraph
from src.status_codes import ResourceLimitExceededError, StatusCode


async def make_graph():
    g = NutmegGraph(fakeredis.FakeRedis())
    for node_id in [
        "viewer",
        "alt_viewer",
        "alice",
        "bob",
        "cara",
        "dana",
        "erin",
        "post1",
        "post2",
    ]:
        await g.add_node(node_id, "person", {"name": node_id})

    for source, target, edge_type, score in [
        ("viewer", "alice", "connected_to", 10),
        ("viewer", "bob", "connected_to", 20),
        ("viewer", "cara", "connected_to", 30),
        ("viewer", "erin", "blocks", 5),
        ("alt_viewer", "dana", "connected_to", 1),
        ("alt_viewer", "bob", "connected_to", 15),
        ("alice", "post1", "posted", 100),
        ("bob", "post2", "posted", 200),
    ]:
        await g.add_edge(source, target, edge_type, score=score)
    return g


async def execute(plan):
    return await QueryExecutor(await make_graph()).execute(plan)


async def test_follow_stage_uses_score_window_and_scores():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer", "alt_viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "connected",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                    "start": 10,
                    "end": 30,
                    "scores": True,
                },
            ],
        }
    )

    assert result["stages"]["connected"] == ["alice", "bob", "cara"]
    assert result["scores"]["connected"] == {
        "alice": 10.0,
        "bob": 15.0,
        "cara": 30.0,
    }


async def test_client_plan_executes_on_the_server():
    query = NutmegClient("http://nutmeg.test").query("viewer")
    connected = query.follow_edges("connected_to", name="connected", scores=True)
    blocked = query.follow_edges("blocks", name="blocked")
    connected.subtract(blocked, name="visible", attributes=True)

    result = await execute(query.to_dict())

    assert result["stages"]["visible"] == ["alice", "bob", "cara"]
    assert "visible" not in result["scores"]


async def test_set_operations_preserve_left_hand_call_order_and_can_feed_follow_stage():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "connected",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                    "scores": True,
                },
                {
                    "name": "blocked",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "blocks",
                },
                {
                    "name": "unioned",
                    "kind": "union",
                    "sources": ["connected", "blocked"],
                },
                {
                    "name": "visible",
                    "kind": "subtract",
                    "sources": ["unioned", "blocked"],
                    "attributes": True,
                    "degrees": True,
                },
                {
                    "name": "also_connected",
                    "kind": "intersect",
                    "sources": ["unioned", "connected"],
                },
                {
                    "name": "changed",
                    "kind": "symmetric_difference",
                    "sources": ["connected", "blocked"],
                },
                {
                    "name": "posts",
                    "kind": "follow",
                    "sources": ["visible"],
                    "edge_type": "posted",
                },
            ],
        }
    )

    assert result["stages"]["unioned"] == ["alice", "bob", "cara", "erin"]
    assert "unioned" not in result["scores"]
    assert result["stages"]["visible"] == ["alice", "bob", "cara"]
    assert result["stages"]["also_connected"] == ["alice", "bob", "cara"]
    assert result["stages"]["changed"] == ["alice", "bob", "cara", "erin"]
    assert result["stages"]["posts"] == ["post1", "post2"]
    assert result["nodes"]["bob"] == {
        "node_type": "person",
        "attributes": {"name": "bob"},
        "degree": {"total": 1, "by_type": {"posted": 1}},
    }


async def test_named_empty_stage_is_present_in_response():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "none",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "missing_edge_type",
                },
            ],
        }
    )

    assert result["stages"]["none"] == []


async def test_metadata_requests_union_across_stages():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "attrs",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                    "attributes": True,
                },
                {
                    "name": "degrees",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                    "degrees": True,
                },
            ],
        }
    )

    assert result["nodes"]["alice"] == {
        "node_type": "person",
        "attributes": {"name": "alice"},
        "degree": {"total": 1, "by_type": {"posted": 1}},
    }


async def test_invalid_query_plan_raises_value_error_before_execution():
    with pytest.raises(ValueError, match="Unknown stage kind"):
        await execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "bad", "kind": "collapse", "sources": ["start_stage"]},
                ],
            }
        )


async def test_nonexistent_start_node_is_rejected_before_execution():
    with pytest.raises(ValueError, match="does not exist"):
        await execute(
            {
                "wire_version": 1,
                "start_nodes": ["ghost"],
                "stage_specs": [{"name": "start_stage", "kind": "start"}],
            }
        )


async def test_unknown_wire_fields_are_rejected_before_follow_execution():
    with pytest.raises(ValueError, match="Unknown stage fields"):
        await execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "bad",
                        "kind": "follow",
                        "sources": ["start_stage"],
                        "edge_type": "connected_to",
                        "max_edges": 0,
                    },
                ],
            }
        )


async def test_set_stage_scores_are_rejected_before_execution():
    with pytest.raises(ValueError, match="cannot request scores"):
        await execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "connected",
                        "kind": "follow",
                        "sources": ["start_stage"],
                        "edge_type": "connected_to",
                    },
                    {
                        "name": "blocked",
                        "kind": "follow",
                        "sources": ["start_stage"],
                        "edge_type": "blocks",
                    },
                    {
                        "name": "bad",
                        "kind": "union",
                        "sources": ["connected", "blocked"],
                        "scores": True,
                    },
                ],
            }
        )


async def test_set_operations_handle_overlapping_and_empty_inputs():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "left",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                },
                {
                    "name": "right",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "blocks",
                },
                {"name": "unioned", "kind": "union", "sources": ["left", "right"]},
                {
                    "name": "symmetric",
                    "kind": "symmetric_difference",
                    "sources": ["left", "right"],
                },
                {"name": "intersection", "kind": "intersect", "sources": ["left", "right"]},
                {"name": "empty", "kind": "subtract", "sources": ["left", "left"]},
            ],
        }
    )

    assert result["stages"]["unioned"] == ["alice", "bob", "cara", "erin"]
    assert result["stages"]["symmetric"] == ["alice", "bob", "cara", "erin"]
    assert result["stages"]["intersection"] == []
    assert result["stages"]["empty"] == []


async def test_symmetric_difference_excludes_nodes_present_in_both_inputs():
    result = await execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer"],
            "stage_specs": [
                {"name": "start_stage", "kind": "start"},
                {
                    "name": "left",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                },
                {
                    "name": "same",
                    "kind": "follow",
                    "sources": ["start_stage"],
                    "edge_type": "connected_to",
                },
                {"name": "unioned", "kind": "union", "sources": ["left", "same"]},
                {
                    "name": "symmetric",
                    "kind": "symmetric_difference",
                    "sources": ["left", "same"],
                },
            ],
        }
    )

    assert result["stages"]["unioned"] == ["alice", "bob", "cara"]
    assert result["stages"]["symmetric"] == []


async def test_result_larger_than_max_result_nodes_raises_resource_limit_exceeded():
    """Contract: a plan whose metadata-requesting stages touch more nodes than
    max_result_nodes is rejected with RESOURCE_LIMIT_EXCEEDED/RESULT_TOO_LARGE
    instead of silently returning a huge response."""
    executor = QueryExecutor(await make_graph(), max_result_nodes=1)

    with pytest.raises(ResourceLimitExceededError) as exc:
        await executor.execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer", "alt_viewer"],
                "stage_specs": [{"name": "start_stage", "kind": "start", "attributes": True}],
            }
        )

    assert exc.value.code == StatusCode.RESOURCE_LIMIT_EXCEEDED
    assert exc.value.reason == "RESULT_TOO_LARGE"


async def test_result_within_max_result_nodes_succeeds():
    """Boundary: exactly max_result_nodes nodes is not rejected."""
    executor = QueryExecutor(await make_graph(), max_result_nodes=2)

    result = await executor.execute(
        {
            "wire_version": 1,
            "start_nodes": ["viewer", "alt_viewer"],
            "stage_specs": [{"name": "start_stage", "kind": "start", "attributes": True}],
        }
    )

    assert set(result["nodes"]) == {"viewer", "alt_viewer"}


class _SlowGraph:
    """Wraps a NutmegGraph so get_node takes `delay` seconds -- lets timeout
    tests exercise QueryExecutor's asyncio.wait_for without racing real time."""

    def __init__(self, graph, delay):
        self._graph = graph
        self._delay = delay

    def __getattr__(self, name):
        return getattr(self._graph, name)

    async def get_node(self, node_id):
        await asyncio.sleep(self._delay)
        return await self._graph.get_node(node_id)


async def test_query_exceeding_timeout_raises_resource_limit_exceeded():
    """Contract: a query that runs longer than timeout_seconds is cancelled and
    reported as RESOURCE_LIMIT_EXCEEDED/TIMEOUT, not left to hang the caller."""
    slow_graph = _SlowGraph(await make_graph(), delay=0.05)
    executor = QueryExecutor(slow_graph, timeout_seconds=0.01)

    with pytest.raises(ResourceLimitExceededError) as exc:
        await executor.execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer"],
                "stage_specs": [{"name": "start_stage", "kind": "start"}],
            }
        )

    assert exc.value.code == StatusCode.RESOURCE_LIMIT_EXCEEDED
    assert exc.value.reason == "TIMEOUT"
