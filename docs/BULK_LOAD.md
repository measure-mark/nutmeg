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
zero-based position in the corresponding input list. `code` is `OK` if
`errors` is empty, else `BULK_PARTIAL_FAILURE` -- see
[status_codes.md](status_codes.md):

```python
{
    "nodes_loaded": 2,
    "edges_loaded": 1,
    "errors": [],
    "code": "OK",
}
```

## Errors and retries

`errors` contains `kind` (`node` or `edge`), `index`, and `message` for a
per-record rejection: a node whose type conflicts with what's already stored,
or an edge whose source or target doesn't exist at the moment its script runs.
Successful records are idempotent upserts, so callers may correct failed input
and retry it -- the response still reflects everything else in the request
that did load.

Malformed request documents and invalid identifiers reject the request before
Redis writes begin.

A Redis-level failure mid-batch (e.g. a lost connection) is different from a
per-record rejection: since batches are non-transactional pipelines, a failure
partway through leaves no way to tell which of that batch's writes actually
landed. Rather than guess, the load stops immediately -- no further batches are
attempted -- and raises `BulkRedisLoader`'s `BulkLoadInterruptedError`, whose
`nodes_loaded`/`edges_loaded`/`errors` are exactly the result of every batch
that completed *before* the failed one (batches run strictly in order). That's
the safe resume point: every write is an idempotent upsert, so retrying the
load from there -- or from scratch -- is always safe. Through the HTTP API this
surfaces as `SERVICE_UNAVAILABLE` (see [status_codes.md](status_codes.md)),
with the partial counts folded into its `detail` text.
