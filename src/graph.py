"""Simple directed graph API backed by Redis.

Nodes and edges are typed; edges optionally carry a score (e.g. a timestamp,
used for ordering) and free-form attributes. Only out-edges are kept in the
query-optimized data structures (zsets) -- in-edges are tracked in a
reverse-edge hint list used to clean up the other side of an edge when its
target node is deleted. The hint is written atomically with the edge; stale
entries left by delete_edge are tolerated. See nutmeg/CLAUDE.md and the plan
this was built from for the full key layout and rationale.
"""

import json

from src import keys, meta_graph
from src.graph_writes import GraphWriteScripts, check_identifier as _check_identifier
from src.status_codes import NodeNotFoundError


class NodeTypeConflictError(ValueError):
    """add_node's node_type doesn't match the id's existing (immutable) type.
    Plain ValueError, not a NutmegError -- see graph_writes.py's module
    docstring for why graph.py stays protocol-agnostic; src/api/server.py
    translates this into the wire-level DataError/NODE_TYPE_CONFLICT response.
    """


# Deletes a node and both sides of its edges, all
# as one server-side script. A crash between separate client round-trips could leave
# the node gone but its edges still dangling (or the reverse); a single EVAL is atomic
# from Redis's point of view and its effects replicate/persist to AOF as one unit, so
# there's no window where only part of the deletion has taken hold.
#
# Key names are computed in Lua from ARGV[1] rather than declared via KEYS, since the
# set of keys touched (one per source of an in-edge) isn't known until the script runs.
# That's fine for a single Redis instance (see docker-compose.yml) but would need
# reworking for Redis Cluster, where every touched key must hash to the same slot.
_DELETE_NODE_LUA = meta_graph.COUNTER_LUA + ("""
local node_id = ARGV[1]
local node_key = 'nutmeg:nodes:' .. node_id
local node_type = redis.call('HGET', node_key, 'node_type')
if not node_type then
    return 0
end

local edge_types_key = 'nutmeg:edge_types:' .. node_id
-- Out-edges must be removed first: for a self-loop this decrements once, then the
-- in-edge pass sees the deleted zset and its ZREM returns 0.
for _, edge_type in ipairs(redis.call('SMEMBERS', edge_types_key)) do
    local edges_key = 'nutmeg:edges:' .. node_id .. ':' .. edge_type
    for _, target_id in ipairs(redis.call('ZRANGE', edges_key, 0, -1)) do
        local target_type = redis.call('HGET', 'nutmeg:nodes:' .. target_id, 'node_type')
        -- A missing type means the primary graph was already corrupt. The tuple is
        -- unknowable, so do not decrement a potentially unrelated counter.
        if target_type then
            change_edge_counters(node_type, target_type, edge_type, -1)
        end
        redis.call('DEL', 'nutmeg:edge_attrs:' .. node_id .. ':' .. edge_type .. ':' .. target_id)
    end
    redis.call('DEL', edges_key)
end
redis.call('DEL', edge_types_key)

-- Entries here may be stale because delete_edge doesn't trim this list, so
-- zrem/zcard below tolerate misses.
-- ':' is safe as the entry's internal delimiter here because both edge_type and
-- source_node are validated colon-free before an edge is ever written (see
-- keys.is_valid_identifier), so exactly one ':' ever appears in an entry.
local in_edges_key = 'nutmeg:in_edges:' .. node_id
for _, entry in ipairs(redis.call('LRANGE', in_edges_key, 0, -1)) do
    local sep = string.find(entry, ':', 1, true)
    local edge_type = string.sub(entry, 1, sep - 1)
    local source_node = string.sub(entry, sep + 1)
    local source_edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
    local removed = redis.call('ZREM', source_edges_key, node_id)
    if removed == 1 then
        local source_type = redis.call('HGET', 'nutmeg:nodes:' .. source_node, 'node_type')
        -- A missing source type means the primary graph was already corrupt and
        -- the relationship tuple can no longer be identified safely.
        if source_type then
            change_edge_counters(source_type, node_type, edge_type, -1)
        end
    end
    redis.call('DEL', 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. node_id)
    if redis.call('ZCARD', source_edges_key) == 0 then
        redis.call('SREM', 'nutmeg:edge_types:' .. source_node, edge_type)
    end
end
redis.call('DEL', in_edges_key)
redis.call('DEL', node_key)
change_counter('%s', node_type, -1)
return 1
""" % keys.META_NODE_COUNTS)

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
_DELETE_EDGE_LUA = meta_graph.COUNTER_LUA + """
local source_node = ARGV[1]
local target_node = ARGV[2]
local edge_type = ARGV[3]

local edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
local removed = redis.call('ZREM', edges_key, target_node)
redis.call('DEL', 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. target_node)
if removed == 1 then
    local source_type = redis.call('HGET', 'nutmeg:nodes:' .. source_node, 'node_type')
    local target_type = redis.call('HGET', 'nutmeg:nodes:' .. target_node, 'node_type')
    -- Missing types mean the primary graph was already corrupt. Without both
    -- tuple dimensions, decrementing would risk corrupting a different bucket.
    if source_type and target_type then
        change_edge_counters(source_type, target_type, edge_type, -1)
    end
end
if redis.call('ZCARD', edges_key) == 0 then
    redis.call('SREM', 'nutmeg:edge_types:' .. source_node, edge_type)
end
return 1
"""


def _decode_set(values) -> set:
    return {v.decode() for v in values}


class NutmegGraph:
    def __init__(self, redis_client):
        self._r = redis_client
        self._writes = GraphWriteScripts(redis_client)

    # -- nodes ---------------------------------------------------------

    async def add_node(self, node_id: str, node_type: str, attributes: dict | None = None) -> None:
        """Upsert a node. Its type is immutable; attributes may be replaced."""
        result = await self._writes.add_node(self._r, node_id, node_type, attributes)
        if error := self._writes.add_node_error(node_id, result):
            raise NodeTypeConflictError(error)

    async def get_node(self, node_id: str) -> dict:
        """A node's type, attributes, and out-degree in one call. Reuses get_degree
        rather than re-deriving it, at the cost of one extra (cheap) EXISTS check.

        Raises ValueError if node_id is malformed or the node hasn't been added.
        """
        _check_identifier(node_id, "node_id")
        node = await self._r.hgetall(keys.node_key(node_id))
        if not node:
            raise NodeNotFoundError(f"node {node_id!r} does not exist")

        return {
            "node_type": node[b"node_type"].decode(),
            "attributes": json.loads(node[b"attributes"]),
            "degree": await self.get_degree(node_id),
        }

    async def delete_node(self, node_id: str) -> None:
        """Remove a node and its incoming and outgoing edges. No-op if absent.

        Atomic: runs as a single Lua script so it can't be interrupted partway,
        leaving the node deleted but edges still pointing at it (or vice versa).
        """
        _check_identifier(node_id, "node_id")

        # Called directly with eval() rather than through a registered Script wrapper --
        # this script is only ever used here, so there's nothing to gain from stashing
        # a one-line callable on self just to call it once.
        await self._r.eval(_DELETE_NODE_LUA, 0, node_id)

    # -- edges -----------------------------------------------------------

    async def add_edge(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
        attributes: dict | None = None,
        score: float = 0,
    ) -> None:
        """Upsert a directed edge. Idempotent: re-adding updates score/attributes in place.

        Atomic: runs as a single Lua script (see graph_writes._ADD_EDGE_LUA) so the
        zset entry, the edge_types index, the attrs key, and the in_edges hint all
        land together -- and so the source/target existence check below can't race
        a concurrent delete.

        Raises ValueError if source_node or target_node hasn't been added yet.
        """
        result = await self._writes.add_edge(
            self._r,
            source_node,
            target_node,
            edge_type,
            attributes,
            score,
        )
        if error := self._writes.add_edge_error(source_node, target_node, result):
            raise NodeNotFoundError(error)

    async def delete_edge(self, source_node: str, target_node: str, edge_type: str) -> None:
        """Remove a directed edge. No-op if it doesn't exist.

        Atomic: runs as a single Lua script (see _DELETE_EDGE_LUA) so the zset entry,
        the attrs key, and the edge_types index all update together.
        """
        _check_identifier(source_node, "node_id")
        _check_identifier(target_node, "node_id")
        _check_identifier(edge_type, "edge_type")

        # Same reasoning as delete_node: called directly with eval(), no registered
        # Script wrapper, since nothing else calls this script.
        await self._r.eval(_DELETE_EDGE_LUA, 0, source_node, target_node, edge_type)

    # -- meta graph -------------------------------------------------------

    async def get_meta_graph(self) -> dict:
        """Counts of live node types and typed edge relationships in one snapshot."""
        snapshot = await self._r.eval(meta_graph.GET_META_GRAPH_LUA, 0)
        return meta_graph.decode_snapshot(snapshot)

    # -- queries -----------------------------------------------------------

    async def get_degree(self, node_id: str, edge_type: str | None = None):
        """Out-degree. A single count for one edge_type, else {total, by_type}.

        Raises ValueError if node_id/edge_type is malformed or the node hasn't been added.
        """
        _check_identifier(node_id, "node_id")
        if not await self._r.exists(keys.node_key(node_id)):
            raise NodeNotFoundError(f"node {node_id!r} does not exist")

        if edge_type is not None:
            _check_identifier(edge_type, "edge_type")
            return await self._r.zcard(keys.edges_key(node_id, edge_type))

        by_type = {
            et: await self._r.zcard(keys.edges_key(node_id, et))
            for et in _decode_set(await self._r.smembers(keys.edge_types_key(node_id)))
        }
        return {"total": sum(by_type.values()), "by_type": by_type}

    async def get_neighbors(
        self,
        node_id: str,
        edge_types: list[str] | None = None,
        *,
        start: float | None = None,
        end: float | None = None,
        with_scores: bool = False,
    ) -> list:
        """Out-neighbors ordered by score ascending -- the zsets' native order, which is
        the whole reason we store edges in one. A neighbor reachable via more than one
        edge_type is deduped to its lowest score. Ties (e.g. the default score of 0)
        break by node_id, since edge_types can come back from a Redis SET whose
        iteration order isn't guaranteed. Empty/None edge_types means all types.
        start/end are inclusive score bounds.

        Raises ValueError if node_id/edge_types is malformed or the node hasn't been added.
        """
        _check_identifier(node_id, "node_id")
        if not await self._r.exists(keys.node_key(node_id)):
            raise NodeNotFoundError(f"node {node_id!r} does not exist")
        for edge_type in edge_types or []:
            _check_identifier(edge_type, "edge_type")

        types = edge_types or _decode_set(await self._r.smembers(keys.edge_types_key(node_id)))
        best_score: dict[str, float] = {}
        min_score = "-inf" if start is None else start
        max_score = "+inf" if end is None else end

        for edge_type in types:
            scored = await self._r.zrangebyscore(
                keys.edges_key(node_id, edge_type),
                min_score,
                max_score,
                withscores=True,
            )
            for member, score in scored:
                neighbor = member.decode()
                if neighbor not in best_score or score < best_score[neighbor]:
                    best_score[neighbor] = score
        ordered = sorted(best_score.items(), key=lambda kv: (kv[1], kv[0]))
        if with_scores:
            return [{"node_id": neighbor, "score": score} for neighbor, score in ordered]
        return [neighbor for neighbor, _ in ordered]
