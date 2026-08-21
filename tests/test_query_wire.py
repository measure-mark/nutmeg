"""Tests for the query wire contract shared by client and server."""

import pytest

from src.query_wire import load_query_wire


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
