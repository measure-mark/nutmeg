"""Contracts for fast, deliberately non-transactional graph loading."""

import fakeredis.aioredis as fakeredis
import pytest
import redis.exceptions

from src.bulk_redis import BulkLoadInterruptedError, BulkRedisLoader, MAX_BATCH_SIZE
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

    assert result == {"nodes_loaded": 2, "edges_loaded": 1, "errors": [], "code": "OK"}
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
        "code": "BULK_PARTIAL_FAILURE",
    }
    assert await graph.get_neighbors("existing") == ["new"]


class _FailAfterNPipelineRedis:
    """Wraps a real redis client so pipeline() succeeds `calls_before_failure`
    times, then raises a connection error on every call after that -- lets a
    test put a Redis-level failure at a specific batch, with earlier batches
    still going through fakeredis for real."""

    def __init__(self, real, calls_before_failure):
        self._real = real
        self._calls_before_failure = calls_before_failure

    def pipeline(self, transaction=False):
        if self._calls_before_failure <= 0:
            raise redis.exceptions.ConnectionError("connection refused")
        self._calls_before_failure -= 1
        return self._real.pipeline(transaction=transaction)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_bulk_load_stops_immediately_on_a_lost_connection():
    """Contract: a Redis-level failure mid-batch (a lost connection, not a
    per-record rejection like a type conflict) stops the whole load rather than
    silently skipping the ambiguous batch and continuing on to later ones --
    we can't tell which of the failed batch's writes landed, so continuing
    could report a misleadingly clean partial result. The raised error carries
    exactly what completed *before* the failed batch, which is the caller's
    safe resume point since every write is an idempotent upsert."""
    real_redis = fakeredis.FakeRedis()
    flaky = _FailAfterNPipelineRedis(real_redis, calls_before_failure=1)
    loader = BulkRedisLoader(flaky)
    graph = NutmegGraph(real_redis)

    with pytest.raises(BulkLoadInterruptedError) as exc:
        await loader.load(
            [
                {"node_id": "ada", "node_type": "person"},
                {"node_id": "grace", "node_type": "person"},
                {"node_id": "irene", "node_type": "person"},
            ],
            [],
            batch_size=1,
        )

    assert exc.value.nodes_loaded == 1
    assert exc.value.edges_loaded == 0
    assert exc.value.errors == []
    assert (await graph.get_node("ada"))["node_type"] == "person"
    # Neither the failed batch nor anything after it was attempted.
    with pytest.raises(ValueError):
        await graph.get_node("grace")
    with pytest.raises(ValueError):
        await graph.get_node("irene")


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
