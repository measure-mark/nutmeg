"""Contracts for fast, deliberately non-transactional graph loading."""

import fakeredis.aioredis as fakeredis
import pytest

from src.bulk_redis import BulkRedisLoader, MAX_BATCH_SIZE
from src.graph import NutmegGraph


async def test_bulk_load_writes_nodes_before_edges_and_keeps_graph_invariants():
    redis_client = fakeredis.FakeRedis()
    loader = BulkRedisLoader(redis_client)
    graph = NutmegGraph(redis_client)

    result = await loader.load(
        [
            {"node_id": "ada", "node_type": "person", "attributes": {"name": "Ada"}},
            {"node_id": "celtics", "node_type": "team"},
        ],
        [
            {
                "source_node": "ada",
                "target_node": "celtics",
                "edge_type": "plays_for",
                "score": 7,
                "attributes": {"active": True},
            }
        ],
        batch_size=1,
    )

    assert result == {"nodes_loaded": 2, "edges_loaded": 1, "errors": []}
    assert await graph.get_node("ada") == {
        "node_type": "person",
        "attributes": {"name": "Ada"},
        "degree": {"total": 1, "by_type": {"plays_for": 1}},
    }
    assert await graph.get_meta_graph() == {
        "node_counts": {"person": 1, "team": 1},
        "edge_counts": {"plays_for": 1},
        "node_edge_counts": [
            {"source_type": "person", "edge_type": "plays_for", "count": 1}
        ],
        "node_edge_node_counts": [
            {
                "source_type": "person",
                "edge_type": "plays_for",
                "target_type": "team",
                "count": 1,
            }
        ],
    }


async def test_bulk_load_reports_record_errors_after_other_records_commit():
    redis_client = fakeredis.FakeRedis()
    loader = BulkRedisLoader(redis_client)
    graph = NutmegGraph(redis_client)
    await graph.add_node("existing", "person")

    result = await loader.load(
        [
            {"node_id": "existing", "node_type": "team"},
            {"node_id": "new", "node_type": "person"},
        ],
        [
            {"source_node": "new", "target_node": "missing", "edge_type": "knows"},
            {"source_node": "existing", "target_node": "new", "edge_type": "knows"},
        ],
    )

    assert result == {
        "nodes_loaded": 1,
        "edges_loaded": 1,
        "errors": [
            {
                "kind": "node",
                "index": 0,
                "message": "node 'existing' already has type 'person'",
            },
            {
                "kind": "edge",
                "index": 0,
                "message": "target node 'missing' does not exist",
            },
        ],
    }
    assert await graph.get_neighbors("existing") == ["new"]


async def test_bulk_load_rejects_invalid_configuration_before_writing():
    redis_client = fakeredis.FakeRedis()
    loader = BulkRedisLoader(redis_client)

    with pytest.raises(ValueError, match="batch_size"):
        await loader.load(
            [{"node_id": "ada", "node_type": "person"}],
            [],
            batch_size=MAX_BATCH_SIZE + 1,
        )
    with pytest.raises(ValueError, match="Invalid node_id"):
        await loader.load(
            [{"node_id": "bad:id", "node_type": "person"}],
            [],
        )

    assert not await redis_client.exists("nutmeg:nodes:ada")
