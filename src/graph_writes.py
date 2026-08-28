"""Registered-script contract for graph *writes*, shared by single (NutmegGraph)
and bulk (BulkRedisLoader) upserts so they cannot drift apart when a script
changes. Kept separate from graph.py: batching bulk writes into pipelines is a
bulk-loading concern, not part of graph.py's read/traversal/single-write API.

Like graph.py, this module stays protocol-agnostic -- it raises plain
ValueError subclasses, not Nutmeg status-coded errors. The layer above (see
src/api/server.py's exception handlers) translates them into the wire-level
error contract.
"""

import json

from src import keys, meta_graph


class InvalidIdentifierError(ValueError):
    """A node_id or edge_type contains ':', the delimiter the whole Redis key
    scheme is built on. Plain ValueError, not a NutmegError -- see module
    docstring."""


def check_identifier(value: str, label: str) -> None:
    """Raise InvalidIdentifierError naming the field if value isn't a valid
    node_id/edge_type. One place for this instead of a copy-pasted if/raise at
    every call site."""
    if not keys.is_valid_identifier(value):
        raise InvalidIdentifierError(f"Invalid {label}: {value!r}")


_ADD_NODE_LUA = meta_graph.COUNTER_LUA + ("""
local node_id = ARGV[1]
local node_type = ARGV[2]
local attributes_json = ARGV[3]
local node_key = 'nutmeg:nodes:' .. node_id
local existing_type = redis.call('HGET', node_key, 'node_type')

if existing_type and existing_type ~= node_type then
    return existing_type
end
if not existing_type then
    change_counter('%s', node_type, 1)
end

redis.call('HSET', node_key, 'node_type', node_type, 'attributes', attributes_json)
if existing_type then
    return 0
end
return 1
""" % keys.META_NODE_COUNTS)

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
_ADD_EDGE_LUA = meta_graph.COUNTER_LUA + """
local source_node = ARGV[1]
local target_node = ARGV[2]
local edge_type = ARGV[3]
local score = ARGV[4]
local attributes_json = ARGV[5]  -- empty string is the "no attributes" sentinel

-- Checked here rather than as a separate client-side EXISTS before the script runs,
-- so there's no window where a node gets deleted between the check and the write.
local source_type = redis.call('HGET', 'nutmeg:nodes:' .. source_node, 'node_type')
if not source_type then
    return -1
end
local target_type = redis.call('HGET', 'nutmeg:nodes:' .. target_node, 'node_type')
if not target_type then
    return -2
end

local edges_key = 'nutmeg:edges:' .. source_node .. ':' .. edge_type
local added = redis.call('ZADD', edges_key, score, target_node)
redis.call('SADD', 'nutmeg:edge_types:' .. source_node, edge_type)

local attrs_key = 'nutmeg:edge_attrs:' .. source_node .. ':' .. edge_type .. ':' .. target_node
if attributes_json ~= '' then
    redis.call('SET', attrs_key, attributes_json)
else
    redis.call('DEL', attrs_key)
end

if added == 1 then
    -- Only new edges need a reverse hint; gating this also prevents unbounded
    -- hint-list growth when an existing edge's score or attributes are updated.
    redis.call('RPUSH', 'nutmeg:in_edges:' .. target_node, edge_type .. ':' .. source_node)
    change_edge_counters(source_type, target_type, edge_type, 1)
end
return 1
"""


class GraphWriteScripts:
    """Shared registered-script contract for graph upserts.

    This class owns validation, Lua argument encoding, and response decoding so
    single and bulk writes cannot drift apart when a script changes.
    """

    def __init__(self, redis_client):
        self._add_node = redis_client.register_script(_ADD_NODE_LUA)
        self._add_edge = redis_client.register_script(_ADD_EDGE_LUA)

    @staticmethod
    def validate_node(node_id: str) -> None:
        check_identifier(node_id, "node_id")

    @staticmethod
    def validate_edge(source_node: str, target_node: str, edge_type: str) -> None:
        check_identifier(source_node, "node_id")
        check_identifier(target_node, "node_id")
        check_identifier(edge_type, "edge_type")

    async def add_node(
        self,
        client,
        node_id: str,
        node_type: str,
        attributes: dict | None = None,
    ):
        self.validate_node(node_id)
        return await self._add_node(
            args=[node_id, node_type, json.dumps(attributes or {})],
            client=client,
        )

    async def add_edge(
        self,
        client,
        source_node: str,
        target_node: str,
        edge_type: str,
        attributes: dict | None = None,
        score: float = 0,
    ):
        self.validate_edge(source_node, target_node, edge_type)
        return await self._add_edge(
            args=[
                source_node,
                target_node,
                edge_type,
                score,
                json.dumps(attributes) if attributes else "",
            ],
            client=client,
        )

    @staticmethod
    def add_node_error(node_id: str, response) -> str | None:
        if isinstance(response, bytes):
            return f"node {node_id!r} already has type {response.decode()!r}"
        return None

    @staticmethod
    def add_edge_error(
        source_node: str,
        target_node: str,
        response,
    ) -> str | None:
        """Returns a message for bulk-load's per-record error list. Callers that
        need to raise (see NutmegGraph.add_edge) wrap the message in NodeNotFoundError
        themselves -- bulk load reports the same failure without raising."""
        if response == -1:
            return f"source node {source_node!r} does not exist"
        if response == -2:
            return f"target node {target_node!r} does not exist"
        return None
