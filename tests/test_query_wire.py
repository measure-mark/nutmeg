"""Tests for the query wire contract shared by client and server."""

import sys

import pytest

from src.query_wire import MAX_QUERY_DEPTH, load_query_wire
from src.status_codes import InvalidQueryError, MaxDepthExceededError, StatusCode


def valid_wire():
    return {
        "wire_version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [{"name": "start_stage", "kind": "start"}],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"wire_version": 2}, "Unsupported query wire_version"),
        ({"start_nodes": []}, "at least one start node"),
        ({"start_nodes": [1]}, "start_nodes must be a list of node ids"),
        ({"stage_specs": []}, "at least one stage"),
        (
            {"stage_specs": [{"name": "start_stage", "kind": "start", "sources": ["x"]}]},
            "cannot have sources",
        ),
        (
            {
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "bad", "kind": "follow", "sources": ["start_stage"]},
                ]
            },
            "requires one source and edge_type",
        ),
        (
            {
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "bad",
                        "kind": "union",
                        "sources": ["start_stage", "start_stage"],
                        "scores": True,
                    },
                ]
            },
            "cannot request scores",
        ),
        (
            {
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "a", "kind": "union", "sources": ["b", "start_stage"]},
                    {"name": "b", "kind": "union", "sources": ["a", "start_stage"]},
                ]
            },
            "cycle",
        ),
    ],
)
def test_invalid_wire_payloads_are_rejected(change, message):
    wire = valid_wire()
    wire.update(change)

    with pytest.raises(ValueError, match=message):
        load_query_wire(wire)


@pytest.mark.parametrize(
    "change",
    [
        {"wire_version": 2},
        {"start_nodes": []},
        {"stage_specs": []},
        {"stage_specs": [{"name": "start_stage", "kind": "start", "sources": ["x"]}]},
    ],
)
def test_invalid_wire_payloads_carry_the_invalid_query_status_code(change):
    """Contract: every rejected wire payload is reported as INVALID_QUERY with a
    reason, so a client can switch on `code` without parsing the message text."""
    wire = valid_wire()
    wire.update(change)

    with pytest.raises(InvalidQueryError) as exc:
        load_query_wire(wire)

    assert exc.value.code == StatusCode.INVALID_QUERY
    assert exc.value.reason is not None


def _chained_follow_wire(depth: int) -> dict:
    """A start stage followed by `depth` sequential follow stages -- the plan's
    depth is exactly `depth`."""
    stages = [{"name": "start_stage", "kind": "start"}]
    previous = "start_stage"
    for i in range(depth):
        name = f"stage{i}"
        stages.append(
            {"name": name, "kind": "follow", "sources": [previous], "edge_type": "knows"}
        )
        previous = name
    return {"wire_version": 1, "start_nodes": ["ada"], "stage_specs": stages}


def test_query_wire_accepts_the_maximum_allowed_depth():
    """Boundary: a plan exactly at MAX_QUERY_DEPTH is not rejected."""
    wire = load_query_wire(_chained_follow_wire(MAX_QUERY_DEPTH))
    assert len(wire.stages) == MAX_QUERY_DEPTH + 1


def test_query_wire_rejects_a_plan_one_stage_past_the_maximum_depth():
    with pytest.raises(MaxDepthExceededError) as exc:
        load_query_wire(_chained_follow_wire(MAX_QUERY_DEPTH + 1))

    assert exc.value.code == StatusCode.MAX_DEPTH_EXCEEDED


def test_deeply_chained_wire_declared_deepest_first_does_not_recurse():
    """Regression: depth used to be computed by Python-recursing over each
    stage's sources. Declared deepest-stage-first (reverse topological order),
    that recursion started before memoization had anything cached, so a chain
    longer than sys.getrecursionlimit() raised RecursionError instead of the
    intended MaxDepthExceededError. A wire document's field order must not
    determine whether a client gets a stable application error or a crash."""
    depth = sys.getrecursionlimit() + 500
    wire = _chained_follow_wire(depth)
    wire["stage_specs"] = list(reversed(wire["stage_specs"]))

    with pytest.raises(MaxDepthExceededError):
        load_query_wire(wire)


def test_wire_loader_rejects_unknown_stage_fields():
    wire = valid_wire()
    wire["stage_specs"].append(
        {
            "name": "bad",
            "kind": "follow",
            "sources": ["start_stage"],
            "edge_type": "connected_to",
            "max_edges": 1,
        }
    )

    with pytest.raises(ValueError, match="Unknown stage fields"):
        load_query_wire(wire)


def test_client_wire_round_trip_is_accepted_by_shared_loader():
    from src.client import NutmegClient

    wire = NutmegClient("http://nutmeg.test").query("ada").to_dict()
    loaded = load_query_wire(wire)

    assert loaded.start_nodes == ["ada"]
    assert list(loaded.stages) == ["start_stage"]
