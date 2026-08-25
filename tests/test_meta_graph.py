"""Fast metadata invariants using fakeredis's real Lua execution path."""

import random
from collections import Counter

import fakeredis.aioredis as fakeredis
import pytest

from src.graph import NutmegGraph


async def _recount_meta(redis_client) -> dict:
    """Rebuild the public metadata shape from primary node hashes and edge zsets."""
    node_types = {}
    async for node_key in redis_client.scan_iter(match="nutmeg:nodes:*"):
        node_id = node_key.removeprefix(b"nutmeg:nodes:").decode()
        node_types[node_id] = (await redis_client.hget(node_key, "node_type")).decode()

    edge_counts = Counter()
    node_edge_counts = Counter()
    node_edge_node_counts = Counter()
    async for edge_key in redis_client.scan_iter(match="nutmeg:edges:*"):
        _, _, source_id, edge_type = edge_key.decode().split(":", 3)
        source_type = node_types[source_id]
        for target_id_bytes in await redis_client.zrange(edge_key, 0, -1):
            target_type = node_types[target_id_bytes.decode()]
            edge_counts[edge_type] += 1
            node_edge_counts[(source_type, edge_type)] += 1
            node_edge_node_counts[(source_type, edge_type, target_type)] += 1

    node_counts = Counter(node_types.values())
    return {
        "node_counts": dict(sorted(node_counts.items())),
        "edge_counts": dict(sorted(edge_counts.items())),
        "node_edge_counts": [
            {"source_type": source_type, "edge_type": edge_type, "count": count}
            for (source_type, edge_type), count in sorted(node_edge_counts.items())
        ],
        "node_edge_node_counts": [
            {"source_type": source_type, "edge_type": edge_type, "target_type": target_type, "count": count}
            for (source_type, edge_type, target_type), count in sorted(node_edge_node_counts.items())
        ],
    }


async def test_meta_graph_tracks_idempotency_cascades_and_self_loops():
    """Business rule: every successful mutation leaves all four metadata views exact."""
    redis_client = fakeredis.FakeRedis()
    graph = NutmegGraph(redis_client)

    await graph.add_node("p1", "person")
    await graph.add_node("p1", "person")
    await graph.add_node("p2", "person")
    await graph.add_node("team", "team")
    with pytest.raises(ValueError, match="already has type 'person'"):
        await graph.add_node("p1", "team")

    await graph.add_edge("p1", "team", "member")
    await graph.add_edge("p1", "team", "member", score=2)
    await graph.add_edge("p2", "team", "member")
    await graph.add_edge("p1", "p1", "self")
    await graph.delete_edge("p2", "team", "absent")

    assert await graph.get_meta_graph() == await _recount_meta(redis_client) == {
        "node_counts": {"person": 2, "team": 1},
        "edge_counts": {"member": 2, "self": 1},
        "node_edge_counts": [
            {"source_type": "person", "edge_type": "member", "count": 2},
            {"source_type": "person", "edge_type": "self", "count": 1},
        ],
        "node_edge_node_counts": [
            {"source_type": "person", "edge_type": "member", "target_type": "team", "count": 2},
            {"source_type": "person", "edge_type": "self", "target_type": "person", "count": 1},
        ],
    }

    await graph.delete_node("team")
    assert await graph.get_meta_graph() == await _recount_meta(redis_client) == {
        "node_counts": {"person": 2},
        "edge_counts": {"self": 1},
        "node_edge_counts": [{"source_type": "person", "edge_type": "self", "count": 1}],
        "node_edge_node_counts": [
            {"source_type": "person", "edge_type": "self", "target_type": "person", "count": 1}
        ],
    }

    await graph.delete_node("p1")
    assert await graph.get_meta_graph() == await _recount_meta(redis_client) == {
        "node_counts": {"person": 1},
        "edge_counts": {},
        "node_edge_counts": [],
        "node_edge_node_counts": [],
    }


async def test_meta_graph_matches_primary_data_across_randomized_mutations():
    """Regression net: fixed random traces catch counter drift between mutation combinations."""
    node_ids = [f"n{index}" for index in range(8)]
    node_types = {node_id: ("person" if index < 4 else "group") for index, node_id in enumerate(node_ids)}
    edge_types = ["member", "follows", "likes"]

    for seed in range(8):
        redis_client = fakeredis.FakeRedis()
        graph = NutmegGraph(redis_client)
        active = set()
        randomizer = random.Random(seed)

        for _ in range(150):
            operation = randomizer.choice(("add_node", "add_edge", "delete_edge", "delete_node"))
            node_id = randomizer.choice(node_ids)
            if operation == "add_node":
                await graph.add_node(node_id, node_types[node_id])
                active.add(node_id)
            elif operation == "add_edge" and active:
                source_id = randomizer.choice(sorted(active))
                target_id = randomizer.choice(sorted(active))
                await graph.add_edge(
                    source_id,
                    target_id,
                    randomizer.choice(edge_types),
                    score=randomizer.randrange(10),
                )
            elif operation == "delete_edge":
                await graph.delete_edge(
                    randomizer.choice(node_ids),
                    randomizer.choice(node_ids),
                    randomizer.choice(edge_types),
                )
            elif operation == "delete_node":
                await graph.delete_node(node_id)
                active.discard(node_id)

            assert await graph.get_meta_graph() == await _recount_meta(redis_client), (
                f"metadata drift after seed={seed}, operation={operation}"
            )
