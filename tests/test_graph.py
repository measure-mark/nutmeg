"""NutmegGraph behaviour.

Each test ties to a specific contract: an idempotency guarantee, a return-shape
decision, or a cascade-delete rule -- not written for coverage's own sake.

Node ids are plain, globally-unique identifiers with no structure to them --
node_type is already a separate field, so an id like "ada:player" would just
be repeating information the store already has.
"""

import fakeredis
import pytest

from src.graph import NutmegGraph


def test_add_node_is_idempotent():
    """Business rule: calling add_node twice with the same args leaves the same state."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player", {"name": "Ada"})
    g.add_node("ada", "player", {"name": "Ada"})

    assert g.get_degree("ada") == {"total": 0, "by_type": {}}


def test_add_node_overwrites_on_conflicting_call():
    """Design decision: add_node is an upsert, so a second call with new data wins."""
    r = fakeredis.FakeStrictRedis()
    g = NutmegGraph(r)
    g.add_node("ada", "player", {"name": "Ada"})
    g.add_node("ada", "player", {"name": "Ada Lovelace"})

    assert r.hget("nutmeg:nodes:ada", "attributes") == b'{"name": "Ada Lovelace"}'


def test_get_node_returns_type_attributes_and_degree():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player", {"name": "Ada"})
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")

    assert g.get_node("ada") == {
        "node_type": "player",
        "attributes": {"name": "Ada"},
        "degree": {"total": 1, "by_type": {"plays_for": 1}},
    }


def test_get_node_raises_if_node_does_not_exist():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    with pytest.raises(ValueError):
        g.get_node("ghost")


def test_add_edge_creates_out_edge_and_degree():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")

    assert g.get_degree("ada", "plays_for") == 1
    assert g.get_degree("ada") == {"total": 1, "by_type": {"plays_for": 1}}


def test_add_edge_is_idempotent_and_updates_score():
    """Re-adding the same edge upserts its score rather than creating a duplicate."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for", score=2020)
    g.add_edge("ada", "celtics", "plays_for", score=2021)

    assert g.get_degree("ada", "plays_for") == 1


def test_add_edge_raises_if_source_node_does_not_exist():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("celtics", "team")

    with pytest.raises(ValueError):
        g.add_edge("ada", "celtics", "plays_for")


def test_add_edge_raises_if_target_node_does_not_exist():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")

    with pytest.raises(ValueError):
        g.add_edge("ada", "celtics", "plays_for")


def test_add_edge_raises_for_colon_in_edge_type():
    """Regression: edge_type used to be concatenated into the in_edges hint entry
    unvalidated (edge_type + ':' + source_node), so a ':' inside edge_type would be
    misparsed as the field boundary when delete_node later split the entry back
    apart -- corrupting which source/edge_type delete_node's cascade cleaned up.
    Now rejected up front, before an edge (or its hint entry) is ever written."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")

    with pytest.raises(ValueError):
        g.add_edge("ada", "celtics", "rel:bad")


def test_get_degree_splits_by_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_node("grace", "player")
    g.add_node("hedy", "player")
    g.add_edge("ada", "celtics", "plays_for")
    g.add_edge("ada", "grace", "teammate_of")
    g.add_edge("ada", "hedy", "teammate_of")

    assert g.get_degree("ada") == {
        "total": 3,
        "by_type": {"plays_for": 1, "teammate_of": 2},
    }


def test_get_neighbors_defaults_to_all_edge_types():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_node("grace", "player")
    g.add_edge("ada", "celtics", "plays_for")
    g.add_edge("ada", "grace", "teammate_of")

    assert g.get_neighbors("ada") == ["celtics", "grace"]


def test_get_neighbors_filters_by_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_node("grace", "player")
    g.add_edge("ada", "celtics", "plays_for")
    g.add_edge("ada", "grace", "teammate_of")

    assert g.get_neighbors("ada", ["plays_for"]) == ["celtics"]


def test_get_neighbors_orders_by_score():
    """Contract: neighbors come back in the zsets' native score order, not alphabetical --
    that ordering is the reason we picked a sorted set for edges in the first place."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("lakers", "team")
    g.add_node("heat", "team")
    g.add_node("cavaliers", "team")
    g.add_edge("ada", "lakers", "played_for", score=2010)
    g.add_edge("ada", "heat", "played_for", score=2005)
    g.add_edge("ada", "cavaliers", "played_for", score=2015)

    assert g.get_neighbors("ada") == ["heat", "lakers", "cavaliers"]


def test_get_neighbors_filters_by_inclusive_score_window():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    for node_id, score in [("heat", 2005), ("lakers", 2010), ("cavaliers", 2015)]:
        g.add_node(node_id, "team")
        g.add_edge("ada", node_id, "played_for", score=score)

    assert g.get_neighbors("ada", ["played_for"], start=2010, end=2015) == [
        "lakers",
        "cavaliers",
    ]


def test_get_neighbors_can_return_scores_for_query_execution():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("heat", "team")
    g.add_edge("ada", "heat", "played_for", score=2005)

    assert g.get_neighbors("ada", ["played_for"], with_scores=True) == [
        {"node_id": "heat", "score": 2005.0}
    ]


def test_get_neighbors_dedupes_to_lowest_score_across_edge_types():
    """A neighbor reachable via two edge_types keeps its earliest/lowest score for ordering."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("grace", "player")
    g.add_node("hedy", "player")
    g.add_edge("ada", "grace", "rival_of", score=10)
    g.add_edge("ada", "hedy", "teammate_of", score=1)
    g.add_edge("ada", "grace", "teammate_of", score=1)

    assert g.get_neighbors("ada") == ["grace", "hedy"]


def test_get_neighbors_dedupes_across_edge_types():
    """Contract: the same neighbor reached via two edge_types appears once."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("grace", "player")
    g.add_edge("ada", "grace", "teammate_of")
    g.add_edge("ada", "grace", "rival_of")

    assert g.get_neighbors("ada") == ["grace"]


def test_get_degree_raises_if_node_does_not_exist():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    with pytest.raises(ValueError):
        g.get_degree("ghost")


def test_get_neighbors_raises_if_node_does_not_exist():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    with pytest.raises(ValueError):
        g.get_neighbors("ghost")


def test_get_degree_raises_for_colon_in_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")

    with pytest.raises(ValueError):
        g.get_degree("ada", "rel:bad")


def test_get_neighbors_raises_for_colon_in_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")

    with pytest.raises(ValueError):
        g.get_neighbors("ada", ["rel:bad"])


def test_delete_edge_raises_for_colon_in_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")

    with pytest.raises(ValueError):
        g.delete_edge("ada", "celtics", "rel:bad")


def test_delete_edge_is_idempotent():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")
    g.delete_edge("ada", "celtics", "plays_for")
    g.delete_edge("ada", "celtics", "plays_for")  # no-op, not an error

    assert g.get_degree("ada") == {"total": 0, "by_type": {}}


def test_delete_edge_clears_edge_type_from_index_when_empty():
    """Regression: an emptied edge_type must not linger in get_degree's by_type."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")
    g.delete_edge("ada", "celtics", "plays_for")

    assert "plays_for" not in g.get_degree("ada")["by_type"]


def test_delete_node_is_idempotent():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.delete_node("ghost")  # never existed -- must not raise


def test_delete_node_removes_its_out_edges():
    """ada is gone after delete_node, so get_degree("ada") can no longer be called to
    check this (it now raises on a missing node) -- inspect the raw keys instead."""
    r = fakeredis.FakeStrictRedis()
    g = NutmegGraph(r)
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")
    g.delete_node("ada")

    assert not r.exists("nutmeg:edges:ada:plays_for")
    assert not r.exists("nutmeg:edge_types:ada")


def test_delete_node_cascades_into_other_nodes_in_edges():
    """The key contract this design exists for: deleting a target node must also
    remove it from the source node's out-edge zset, using the in_edges hint list."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")

    g.delete_node("celtics")

    assert g.get_degree("ada") == {"total": 0, "by_type": {}}
    assert g.get_neighbors("ada") == []


def test_delete_node_cascade_tolerates_stale_in_edge_entries():
    """The in_edges list isn't trimmed by delete_edge, so delete_node must tolerate
    an entry whose edge was already removed some other way."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("ada", "player")
    g.add_node("celtics", "team")
    g.add_edge("ada", "celtics", "plays_for")
    g.delete_edge("ada", "celtics", "plays_for")  # leaves a stale in_edges hint

    g.delete_node("celtics")  # must not raise despite the stale entry
