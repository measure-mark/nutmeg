"""Atomic metadata bookkeeping and decoding for :mod:`src.graph`.

The Lua fragment is prepended to every graph-mutation script.  This keeps the
primary graph mutation and its metadata counters in the same Redis EVAL, rather
than introducing a separate, race-prone metadata write.
"""

import json

from src import keys


COUNTER_LUA = """
local function change_counter(key, field, amount)
    local count = redis.call('HINCRBY', key, field, amount)
    if count == 0 then
        redis.call('HDEL', key, field)
    end
end

local function change_edge_counters(source_type, target_type, edge_type, amount)
    change_counter('%s', edge_type, amount)
    change_counter('%s', cjson.encode({source_type, edge_type}), amount)
    change_counter('%s', cjson.encode({source_type, edge_type, target_type}), amount)
end
""" % (
    keys.META_EDGE_COUNTS,
    keys.META_NODE_EDGE_COUNTS,
    keys.META_NODE_EDGE_NODE_COUNTS,
)


GET_META_GRAPH_LUA = """
return {
    redis.call('HGETALL', '%s'),
    redis.call('HGETALL', '%s'),
    redis.call('HGETALL', '%s'),
    redis.call('HGETALL', '%s')
}
""" % (
    keys.META_NODE_COUNTS,
    keys.META_EDGE_COUNTS,
    keys.META_NODE_EDGE_COUNTS,
    keys.META_NODE_EDGE_NODE_COUNTS,
)


def _decode_hash(values) -> dict[str, int]:
    decoded = {
        values[index].decode(): int(values[index + 1])
        for index in range(0, len(values), 2)
    }
    return dict(sorted(decoded.items()))


def decode_snapshot(values) -> dict:
    """Convert the four Redis hashes returned by ``GET_META_GRAPH_LUA`` to the API shape."""
    node_values, edge_values, node_edge_values, node_edge_node_values = values
    node_edge_counts = []
    for field, count in _decode_hash(node_edge_values).items():
        source_type, edge_type = json.loads(field)
        node_edge_counts.append({"source_type": source_type, "edge_type": edge_type, "count": count})
    node_edge_node_counts = []
    for field, count in _decode_hash(node_edge_node_values).items():
        source_type, edge_type, target_type = json.loads(field)
        node_edge_node_counts.append(
            {"source_type": source_type, "edge_type": edge_type, "target_type": target_type, "count": count}
        )
    node_edge_counts.sort(key=lambda item: (item["source_type"], item["edge_type"]))
    node_edge_node_counts.sort(key=lambda item: (item["source_type"], item["edge_type"], item["target_type"]))
    return {
        "node_counts": _decode_hash(node_values),
        "edge_counts": _decode_hash(edge_values),
        "node_edge_counts": node_edge_counts,
        "node_edge_node_counts": node_edge_node_counts,
    }
