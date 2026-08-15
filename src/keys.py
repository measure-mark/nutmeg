"""Redis key-naming helpers for the graph store.

Kept as one small module so `graph.py` and its tests share a single source of
truth for the key format, instead of formatting keys ad hoc in each place.
"""

def is_valid_node_id(node_id: str) -> bool:
    if ':' in node_id:
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


def in_edge_entry(edge_type: str, source_node: str) -> str:
    return f"{edge_type}|{source_node}"


def parse_in_edge_entry(entry: str) -> tuple[str, str]:
    edge_type, source_node = entry.split("|", 1)
    return edge_type, source_node
