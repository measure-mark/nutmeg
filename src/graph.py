"""Simple directed graph API backed by Redis.

Nodes and edges are typed; edges optionally carry a score (e.g. a timestamp,
used for ordering) and free-form attributes. Only out-edges are kept in the
query-optimized data structures (zsets) -- in-edges are tracked in a
best-effort, append-only list used solely to clean up the other side of an
edge when its target node is deleted. See nutmeg/CLAUDE.md and the plan this
was built from for the full key layout and rationale.
"""

import json

from src import keys


def _decode_set(values) -> set:
    return {v.decode() for v in values}


def _decode_list(values) -> list:
    return [v.decode() for v in values]


class NutmegGraph:
    def __init__(self, redis_client):
        self._r = redis_client

    # -- nodes ---------------------------------------------------------

    def add_node(self, node_id: str, node_type: str, attributes: dict | None = None) -> None:
        """Upsert a node. Idempotent: identical calls leave identical state."""
        self._r.hset(
            keys.node_key(node_id),
            mapping={"node_type": node_type, "attributes": json.dumps(attributes or {})},
        )

    def delete_node(self, node_id: str) -> None:
        """Remove a node, its out-edges, and (best-effort) its in-edges. No-op if absent."""
        if not self._r.exists(keys.node_key(node_id)):
            return

        for edge_type in _decode_set(self._r.smembers(keys.edge_types_key(node_id))):
            edges_key = keys.edges_key(node_id, edge_type)
            for target_id in _decode_set(self._r.zrange(edges_key, 0, -1)):
                self._r.delete(keys.edge_attrs_key(node_id, edge_type, target_id))
            self._r.delete(edges_key)
        self._r.delete(keys.edge_types_key(node_id))

        # Best-effort cleanup of the other side of each in-edge. Entries here may be
        # stale (delete_edge doesn't trim this list) so zrem/zcard tolerate misses.
        for entry in _decode_list(self._r.lrange(keys.in_edges_key(node_id), 0, -1)):
            edge_type, source_node = keys.parse_in_edge_entry(entry)
            source_edges_key = keys.edges_key(source_node, edge_type)
            self._r.zrem(source_edges_key, node_id)
            self._r.delete(keys.edge_attrs_key(source_node, edge_type, node_id))
            if self._r.zcard(source_edges_key) == 0:
                self._r.srem(keys.edge_types_key(source_node), edge_type)

        self._r.delete(keys.in_edges_key(node_id))
        self._r.delete(keys.node_key(node_id))

    # -- edges -----------------------------------------------------------

    def add_edge(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
        attributes: dict | None = None,
        score: float = 0,
    ) -> None:
        """Upsert a directed edge. Idempotent: re-adding updates score/attributes in place."""
        self._r.zadd(keys.edges_key(source_node, edge_type), {target_node: score})
        self._r.sadd(keys.edge_types_key(source_node), edge_type)

        attrs_key = keys.edge_attrs_key(source_node, edge_type, target_node)
        if attributes:
            self._r.set(attrs_key, json.dumps(attributes))
        else:
            self._r.delete(attrs_key)

        self._r.rpush(keys.in_edges_key(target_node), keys.in_edge_entry(edge_type, source_node))

    def delete_edge(self, source_node: str, target_node: str, edge_type: str) -> None:
        """Remove a directed edge. No-op if it doesn't exist."""
        edges_key = keys.edges_key(source_node, edge_type)
        self._r.zrem(edges_key, target_node)
        self._r.delete(keys.edge_attrs_key(source_node, edge_type, target_node))
        if self._r.zcard(edges_key) == 0:
            self._r.srem(keys.edge_types_key(source_node), edge_type)

    # -- queries -----------------------------------------------------------

    def get_degree(self, node_id: str, edge_type: str | None = None):
        """Out-degree. A single count for one edge_type, else {total, by_type}."""
        if edge_type is not None:
            return self._r.zcard(keys.edges_key(node_id, edge_type))

        by_type = {
            et: self._r.zcard(keys.edges_key(node_id, et))
            for et in _decode_set(self._r.smembers(keys.edge_types_key(node_id)))
        }
        return {"total": sum(by_type.values()), "by_type": by_type}

    def get_neighbors(self, node_id: str, edge_types: list[str] | None = None) -> list[str]:
        """Out-neighbors ordered by score ascending -- the zsets' native order, which is
        the whole reason we store edges in one. A neighbor reachable via more than one
        edge_type is deduped to its lowest score. Ties (e.g. the default score of 0)
        break by node_id, since edge_types can come back from a Redis SET whose
        iteration order isn't guaranteed. Empty/None edge_types means all types.
        """
        types = edge_types or _decode_set(self._r.smembers(keys.edge_types_key(node_id)))
        best_score: dict[str, float] = {}
        for edge_type in types:
            scored = self._r.zrange(keys.edges_key(node_id, edge_type), 0, -1, withscores=True)
            for member, score in scored:
                neighbor = member.decode()
                if neighbor not in best_score or score < best_score[neighbor]:
                    best_score[neighbor] = score
        return [neighbor for neighbor, _ in sorted(best_score.items(), key=lambda kv: (kv[1], kv[0]))]
