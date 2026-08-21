"""Server-side query execution over NutmegGraph."""

import asyncio

import fakeredis
import pytest

from src.api.query_engine import QueryExecutor
from src.client import Nutmeg
from src.graph import NutmegGraph


def make_graph():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
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
        g.add_node(node_id, "person", {"name": node_id})

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
        g.add_edge(source, target, edge_type, score=score)
    return g


def execute(plan):
    return asyncio.run(QueryExecutor(make_graph()).execute(plan))


def test_follow_stage_uses_score_window_and_scores():
    result = execute(
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


def test_client_plan_executes_on_the_server():
    query = Nutmeg("http://nutmeg.test").query("viewer")
    connected = query.follow_edges("connected_to", name="connected", scores=True)
    blocked = query.follow_edges("blocks", name="blocked")
    connected.subtract(blocked, name="visible", attributes=True)

    result = execute(query.to_dict())

    assert result["stages"]["visible"] == ["alice", "bob", "cara"]
    assert "visible" not in result["scores"]


def test_set_operations_preserve_left_hand_call_order_and_can_feed_follow_stage():
    result = execute(
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


def test_named_empty_stage_is_present_in_response():
    result = execute(
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


def test_metadata_requests_union_across_stages():
    result = execute(
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


def test_invalid_query_plan_raises_value_error_before_execution():
    with pytest.raises(ValueError, match="Unknown stage kind"):
        execute(
            {
                "wire_version": 1,
                "start_nodes": ["viewer"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "bad", "kind": "collapse", "sources": ["start_stage"]},
                ],
            }
        )


def test_removed_max_edges_field_is_rejected_before_follow_execution():
    with pytest.raises(ValueError, match="Unknown stage fields"):
        execute(
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


def test_set_stage_scores_are_rejected_before_execution():
    with pytest.raises(ValueError, match="cannot request scores"):
        execute(
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
