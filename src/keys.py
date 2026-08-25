"""Redis key names and naming helpers for the graph store.

Kept as one small module so `graph.py` and its tests share a single source of
truth for the key format, instead of formatting keys ad hoc in each place.
"""

def is_valid_identifier(value: str) -> bool:
    """True unless value contains ':', the delimiter this whole key scheme is built
    on (nutmeg:nodes:<node_id>, nutmeg:edges:<node_id>:<edge_type>, the in_edges hint
    entries below, ...). Applies equally to node_ids and edge_types -- both get
    concatenated into keys/values next to a fixed ':', so both must be barred from
    containing one, or a value containing the delimiter could be misparsed as more
    than one field.
    """
    if ':' in value:
        return False
    return True


def node_key(node_id: str) -> str:
    return f"nutmeg:nodes:{node_id}"


def edges_key(node_id: str, edge_type: str) -> str:
    return f"nutmeg:edges:{node_id}:{edge_type}"


def edge_attrs_key(node_id: str, edge_type: str, target_id: str) -> str:
    return f"nutmeg:edge_attrs:{node_id}:{edge_type}:{target_id}"


def edge_types_key(node_id: str) -> str:
    return f"nutmeg:edge_types:{node_id}"


def in_edges_key(node_id: str) -> str:
    return f"nutmeg:in_edges:{node_id}"


META_NODE_COUNTS = "nutmeg:meta:node_counts"
META_EDGE_COUNTS = "nutmeg:meta:edge_counts"
META_NODE_EDGE_COUNTS = "nutmeg:meta:node_edge_counts"
META_NODE_EDGE_NODE_COUNTS = "nutmeg:meta:node_edge_node_counts"


def in_edge_entry(edge_type: str, source_node: str) -> str:
    # Safe to split back apart unambiguously: both fields are validated colon-free
    # (see is_valid_identifier), so exactly one ':' ever appears in the result,
    # regardless of what either field contains otherwise.
    return f"{edge_type}:{source_node}"


def parse_in_edge_entry(entry: str) -> tuple[str, str]:
    edge_type, source_node = entry.split(":", 1)
    return edge_type, source_node
