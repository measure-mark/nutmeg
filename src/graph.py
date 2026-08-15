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

# Deletes a node, its out-edges, and (best-effort) the other side of its in-edges, all
# as one server-side script. A crash between separate client round-trips could leave
# the node gone but its edges still dangling (or the reverse); a single EVAL is atomic
# from Redis's point of view and its effects replicate/persist to AOF as one unit, so
# there's no window where only part of the deletion has taken hold.
#
# Key names are computed in Lua from ARGV[1] rather than declared via KEYS, since the
# set of keys touched (one per source of an in-edge) isn't known until the script runs.
# That's fine for a single Redis instance (see docker-compose.yml) but would need
# reworking for Redis Cluster, where every touched key must hash to the same slot.
_DELETE_NODE_LUA = """
local node_id = ARGV[1]
local node_key = 'nutmeg:nodes:' .. node_id
if redis.call('EXISTS', node_key) == 0 then
    return 0
end

local edge_types_key = 'nutmeg:edge_types:' .. node_id
for _, edge_type in ipairs(redis.call('SMEMBERS', edge_types_key)) do
    local edges_key = 'nutmeg:edges:' .. node_id .. ':' .. edge_type
    for _, target_id in ipairs(redis.call('ZRANGE', edges_key, 0, -1)) do
        redis.call('DEL', 'nutmeg:edge_attrs:' .. node_id .. ':' .. edge_type .. ':' .. target_id)
    end
    redis.call('DEL', edges_key)
end
redis.call('DEL', edge_types_key)

-- Best-effort cleanup of the other side of each in-edge. Entries here may be stale
-- (delete_edge doesn't trim this list), so zrem/zcard below tolerate misses.
-- ':' is safe as the entry's internal delimiter here because both edge_type and
-- source_node are validated colon-free before an edge is ever written (see
-- keys.is_valid_identifier), so exactly one ':' ever appears in an entry.
local in_edges_key = 'nutmeg:in_edges:' .. node_id
for _, entry in ipairs(redis.call('LRANGE', in_edges_key, 0, -1)) do
    local sep = string.find(entry, ':', 1, true)
    local edge_type = string.sub(entry, 1, sep - 1)
    local source_node = string.sub(entry, sep + 1)
    local source_edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
    redis.call('ZREM', source_edges_key, node_id)
    redis.call('DEL', 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. node_id)
    if redis.call('ZCARD', source_edges_key) == 0 then
        redis.call('SREM', 'nutmeg:edge_types:' .. source_node, edge_type)
    end
end
redis.call('DEL', in_edges_key)
redis.call('DEL', node_key)
return 1
"""

# Upserts a directed edge -- the zset entry, the edge_types index, the attrs key, and
# the in_edges hint on the target -- as one script, for the same reason as above: a
# crash between separate round-trips could leave the edge visible in the zset before
# its attributes or in_edges hint were ever written.
#
# Equivalent non-atomic Python, for readers who'd rather not parse Lua:
#
#     def add_edge(self, source_node, target_node, edge_type, attributes=None, score=0):
#         self._r.zadd(keys.edges_key(source_node, edge_type), {target_node: score})
#         self._r.sadd(keys.edge_types_key(source_node), edge_type)
#
#         attrs_key = keys.edge_attrs_key(source_node, edge_type, target_node)
#         if attributes:
#             self._r.set(attrs_key, json.dumps(attributes))
#         else:
#             self._r.delete(attrs_key)
#
#         self._r.rpush(keys.in_edges_key(target_node), keys.in_edge_entry(edge_type, source_node))
_ADD_EDGE_LUA = """
local source_node = ARGV[1]
local target_node = ARGV[2]
local edge_type = ARGV[3]
local score = ARGV[4]
local attributes_json = ARGV[5]  -- empty string is the "no attributes" sentinel

-- Checked here rather than as a separate client-side EXISTS before the script runs,
-- so there's no window where a node gets deleted between the check and the write.
if redis.call('EXISTS', 'nutmeg:nodes:' .. source_node) == 0 then
    return -1
end
if redis.call('EXISTS', 'nutmeg:nodes:' .. target_node) == 0 then
    return -2
end

local edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
redis.call('ZADD', edges_key, score, target_node)
redis.call('SADD', 'nutmeg:edge_types:' .. source_node, edge_type)

local attrs_key = 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. target_node
if attributes_json ~= '' then
    redis.call('SET', attrs_key, attributes_json)
else
    redis.call('DEL', attrs_key)
end

redis.call('RPUSH', 'nutmeg:in_edges:' .. target_node, edge_type .. ':' .. source_node)
return 1
"""

# Removes a directed edge and, if that emptied its zset, drops the edge_type out of the
# index too -- as one script, so a crash can't leave the index claiming an edge_type
# that no longer has any edges (or the reverse).
#
# Equivalent non-atomic Python:
#
#     def delete_edge(self, source_node, target_node, edge_type):
#         edges_key = keys.edges_key(source_node, edge_type)
#         self._r.zrem(edges_key, target_node)
#         self._r.delete(keys.edge_attrs_key(source_node, edge_type, target_node))
#         if self._r.zcard(edges_key) == 0:
#             self._r.srem(keys.edge_types_key(source_node), edge_type)
_DELETE_EDGE_LUA = """
local source_node = ARGV[1]
local target_node = ARGV[2]
local edge_type = ARGV[3]

local edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
redis.call('ZREM', edges_key, target_node)
redis.call('DEL', 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. target_node)
if redis.call('ZCARD', edges_key) == 0 then
    redis.call('SREM', 'nutmeg:edge_types:' .. source_node, edge_type)
end
return 1
"""


def _decode_set(values) -> set:
    return {v.decode() for v in values}


def _check_identifier(value: str, label: str) -> None:
    """Raise ValueError naming the field if value isn't a valid node_id/edge_type.
    One place for this instead of a copy-pasted if/raise at every call site."""
    if not keys.is_valid_identifier(value):
        raise ValueError(f"Invalid {label}: {value!r}")


class NutmegGraph:
    def __init__(self, redis_client):
        self._r = redis_client

    # -- nodes ---------------------------------------------------------

    def add_node(self, node_id: str, node_type: str, attributes: dict | None = None) -> None:
        """Upsert a node. Idempotent: identical calls leave identical state."""
        _check_identifier(node_id, "node_id")
        self._r.hset(
            keys.node_key(node_id),
            mapping={"node_type": node_type, "attributes": json.dumps(attributes or {})},
        )

    def delete_node(self, node_id: str) -> None:
        """Remove a node, its out-edges, and (best-effort) its in-edges. No-op if absent.

        Atomic: runs as a single Lua script so it can't be interrupted partway,
        leaving the node deleted but edges still pointing at it (or vice versa).
        """
        _check_identifier(node_id, "node_id")

        # Called directly with eval() rather than through a registered Script wrapper --
        # this script is only ever used here, so there's nothing to gain from stashing
        # a one-line callable on self just to call it once.
        self._r.eval(_DELETE_NODE_LUA, 0, node_id)

    # -- edges -----------------------------------------------------------

    def add_edge(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
        attributes: dict | None = None,
        score: float = 0,
    ) -> None:
        """Upsert a directed edge. Idempotent: re-adding updates score/attributes in place.

        Atomic: runs as a single Lua script (see _ADD_EDGE_LUA) so the zset entry, the
        edge_types index, the attrs key, and the in_edges hint all land together --
        and so the source/target existence check below can't race a concurrent delete.

        Raises ValueError if source_node or target_node hasn't been added yet.
        """
        _check_identifier(source_node, "node_id")
        _check_identifier(target_node, "node_id")
        _check_identifier(edge_type, "edge_type")

        attributes_json = json.dumps(attributes) if attributes else ""
        result = self._r.eval(
            _ADD_EDGE_LUA, 0, source_node, target_node, edge_type, score, attributes_json
        )
        if result == -1:
            raise ValueError(f"source node {source_node!r} does not exist")
        if result == -2:
            raise ValueError(f"target node {target_node!r} does not exist")

    def delete_edge(self, source_node: str, target_node: str, edge_type: str) -> None:
        """Remove a directed edge. No-op if it doesn't exist.

        Atomic: runs as a single Lua script (see _DELETE_EDGE_LUA) so the zset entry,
        the attrs key, and the edge_types index all update together.
        """
        _check_identifier(source_node, "node_id")
        _check_identifier(target_node, "node_id")
        _check_identifier(edge_type, "edge_type")

        # Same reasoning as delete_node: called directly with eval(), no registered
        # Script wrapper, since nothing else calls this script.
        self._r.eval(_DELETE_EDGE_LUA, 0, source_node, target_node, edge_type)

    # -- queries -----------------------------------------------------------

    def get_degree(self, node_id: str, edge_type: str | None = None):
        """Out-degree. A single count for one edge_type, else {total, by_type}.

        Raises ValueError if node_id/edge_type is malformed or the node hasn't been added.
        """
        _check_identifier(node_id, "node_id")
        if not self._r.exists(keys.node_key(node_id)):
            raise ValueError(f"node {node_id!r} does not exist")

        if edge_type is not None:
            _check_identifier(edge_type, "edge_type")
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

        Raises ValueError if node_id/edge_types is malformed or the node hasn't been added.
        """
        _check_identifier(node_id, "node_id")
        if not self._r.exists(keys.node_key(node_id)):
            raise ValueError(f"node {node_id!r} does not exist")
        for edge_type in edge_types or []:
            _check_identifier(edge_type, "edge_type")

        types = edge_types or _decode_set(self._r.smembers(keys.edge_types_key(node_id)))
        best_score: dict[str, float] = {}

        # TODO--this rescoring is bad it should just be a merged set, but we wont' worry abotu that
        # for now
        for edge_type in types:
            scored = self._r.zrange(keys.edges_key(node_id, edge_type), 0, -1, withscores=True)
            for member, score in scored:
                neighbor = member.decode()
                if neighbor not in best_score or score < best_score[neighbor]:
                    best_score[neighbor] = score
        return [neighbor for neighbor, _ in sorted(best_score.items(), key=lambda kv: (kv[1], kv[0]))]
