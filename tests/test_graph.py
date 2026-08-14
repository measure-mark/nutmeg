"""NutmegGraph behaviour.

Each test ties to a specific contract: an idempotency guarantee, a return-shape
decision, or a cascade-delete rule -- not written for coverage's own sake.
"""

import fakeredis

from src.graph import NutmegGraph


def test_add_node_is_idempotent():
    """Business rule: calling add_node twice with the same args leaves the same state."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("player:1", "player", {"name": "Ada"})
    g.add_node("player:1", "player", {"name": "Ada"})

    assert g.get_degree("player:1") == {"total": 0, "by_type": {}}


def test_add_node_overwrites_on_conflicting_call():
    """Design decision: add_node is an upsert, so a second call with new data wins."""
    r = fakeredis.FakeStrictRedis()
    g = NutmegGraph(r)
    g.add_node("player:1", "player", {"name": "Ada"})
    g.add_node("player:1", "player", {"name": "Grace"})

    assert r.hget("nutmeg:nodes:player:1", "attributes") == b'{"name": "Grace"}'


def test_add_edge_creates_out_edge_and_degree():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("player:1", "player")
    g.add_node("team:BOS", "team")
    g.add_edge("player:1", "team:BOS", "plays_for")

    assert g.get_degree("player:1", "plays_for") == 1
    assert g.get_degree("player:1") == {"total": 1, "by_type": {"plays_for": 1}}


def test_add_edge_is_idempotent_and_updates_score():
    """Re-adding the same edge upserts its score rather than creating a duplicate."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for", score=2020)
    g.add_edge("player:1", "team:BOS", "plays_for", score=2021)

    assert g.get_degree("player:1", "plays_for") == 1


def test_get_degree_splits_by_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.add_edge("player:1", "player:2", "teammate_of")
    g.add_edge("player:1", "player:3", "teammate_of")

    assert g.get_degree("player:1") == {
        "total": 3,
        "by_type": {"plays_for": 1, "teammate_of": 2},
    }


def test_get_neighbors_defaults_to_all_edge_types():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.add_edge("player:1", "player:2", "teammate_of")

    assert g.get_neighbors("player:1") == ["player:2", "team:BOS"]


def test_get_neighbors_filters_by_edge_type():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.add_edge("player:1", "player:2", "teammate_of")

    assert g.get_neighbors("player:1", ["plays_for"]) == ["team:BOS"]


def test_get_neighbors_orders_by_score():
    """Contract: neighbors come back in the zsets' native score order, not alphabetical --
    that ordering is the reason we picked a sorted set for edges in the first place."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:LAL", "played_for", score=2010)
    g.add_edge("player:1", "team:MIA", "played_for", score=2005)
    g.add_edge("player:1", "team:CLE", "played_for", score=2015)

    assert g.get_neighbors("player:1") == ["team:MIA", "team:LAL", "team:CLE"]


def test_get_neighbors_dedupes_to_lowest_score_across_edge_types():
    """A neighbor reachable via two edge_types keeps its earliest/lowest score for ordering."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "player:2", "rival_of", score=10)
    g.add_edge("player:1", "player:3", "teammate_of", score=1)
    g.add_edge("player:1", "player:2", "teammate_of", score=1)

    assert g.get_neighbors("player:1") == ["player:2", "player:3"]


def test_get_neighbors_dedupes_across_edge_types():
    """Contract: the same neighbor reached via two edge_types appears once."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "player:2", "teammate_of")
    g.add_edge("player:1", "player:2", "rival_of")

    assert g.get_neighbors("player:1") == ["player:2"]


def test_delete_edge_is_idempotent():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.delete_edge("player:1", "team:BOS", "plays_for")
    g.delete_edge("player:1", "team:BOS", "plays_for")  # no-op, not an error

    assert g.get_degree("player:1") == {"total": 0, "by_type": {}}


def test_delete_edge_clears_edge_type_from_index_when_empty():
    """Regression: an emptied edge_type must not linger in get_degree's by_type."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.delete_edge("player:1", "team:BOS", "plays_for")

    assert "plays_for" not in g.get_degree("player:1")["by_type"]


def test_delete_node_is_idempotent():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.delete_node("player:999")  # never existed -- must not raise


def test_delete_node_removes_its_out_edges():
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("player:1", "player")
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.delete_node("player:1")

    assert g.get_degree("player:1") == {"total": 0, "by_type": {}}


def test_delete_node_cascades_into_other_nodes_in_edges():
    """The key contract this design exists for: deleting a target node must also
    remove it from the source node's out-edge zset, using the in_edges hint list."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_node("player:1", "player")
    g.add_node("team:BOS", "team")
    g.add_edge("player:1", "team:BOS", "plays_for")

    g.delete_node("team:BOS")

    assert g.get_degree("player:1") == {"total": 0, "by_type": {}}
    assert g.get_neighbors("player:1") == []


def test_delete_node_cascade_tolerates_stale_in_edge_entries():
    """The in_edges list isn't trimmed by delete_edge, so delete_node must tolerate
    an entry whose edge was already removed some other way."""
    g = NutmegGraph(fakeredis.FakeStrictRedis())
    g.add_edge("player:1", "team:BOS", "plays_for")
    g.delete_edge("player:1", "team:BOS", "plays_for")  # leaves a stale in_edges hint

    g.delete_node("team:BOS")  # must not raise despite the stale entry
