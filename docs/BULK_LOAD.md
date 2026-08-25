# Bulk loading

Bulk loading is available through `NutmegClient`; it is not exposed by the MCP
server. The client sends one request to the Nutmeg HTTP service, which writes
all nodes before writing any edges.

```python
from src.client import NutmegClient

nutmeg = NutmegClient("http://127.0.0.1:3879", timeout=60)
result = await nutmeg.bulk_load(
    nodes=[
        {"node_id": "ada", "node_type": "person", "attributes": {"name": "Ada"}},
        {"node_id": "grace", "node_type": "person"},
    ],
    edges=[
        {
            "source_node": "ada",
            "target_node": "grace",
            "edge_type": "knows",
            "score": 1,
            "attributes": {"since": 1843},
        }
    ],
    batch_size=5_000,
)
```

The result counts successful upserts and identifies rejected records by their
zero-based position in the corresponding input list:

```python
{
    "nodes_loaded": 2,
    "edges_loaded": 1,
    "errors": [],
}
```



## Errors and retries

`errors` contains `kind` (`node` or `edge`), `index`, and `message`. Successful
records are idempotent upserts, so callers may correct failed input and retry it.
A node's existing type remains immutable. An edge is rejected when its source or
target node does not exist at the moment its script runs.

Malformed request documents and invalid identifiers reject the request before
Redis writes begin. Infrastructure failures such as a lost Redis connection can
still happen after partial progress; callers should treat the load as resumable
and retry idempotently.
