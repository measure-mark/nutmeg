# Nutmeg

Nutmeg is a graph database using Redis for persistence, optimized for set
operations where edges are *strictly ordered* (e.g. by date). By storing the edges as sorted sets, Nutmeg trades very fast read symantics of exisiting data for slower writes.  Nutmeg features both a Python client and an MCP server.  Both expose a rich traversal language that features set operations and branching. 

It is built for questions like “which connections are within three degrees of separation after subtracting blockers" or 
“show me the 10 most recent concerts at the House of Blues?” A query stage is
a set of node ids. You branch, union, intersect, subtract, and continue
traversing from those sets. Nutmeg intentionally returns compact stage results
instead of full paths.

Nutmeg takes its inspiration from Facebook's TAO and SPiN's Garden, though it differs from both projects significantly. 

## Python Client

The client talks to the HTTP API and has no third-party runtime dependency.

```python
from src.client import NutmegClient

nutmeg = NutmegClient("http://127.0.0.1:3879")

await nutmeg.bulk_load(
    nodes=[
        {"node_id": "ada", "node_type": "person", "attributes": {"name": "Ada"}},
        {"node_id": "grace", "node_type": "person", "attributes": {"name": "Grace"}},
    ],
    edges=[
        {
            "source_node": "ada",
            "target_node": "grace",
            "edge_type": "connected_to",
            "score": 1,
        }
    ],
)

node = await nutmeg.get_node("ada")
degree = await nutmeg.get_degree("ada")
plays_for_degree = await nutmeg.get_degree("ada", "plays_for")
neighbors = await nutmeg.get_neighbors("ada", ["connected_to"], start=10, end=20)
```

## The query builder

Queries are lazy. The client builds a query plan locally, then `execute()` sends
the whole plan to `POST /queries/execute`. Nutmeg executes the traversal on the
server against Redis and returns one packed response.

```python
nutmeg = NutmegClient("http://127.0.0.1:3879")

query = nutmeg.query("ada")
connected = query.follow_edges(
    "connected_to",
    name="connected",
    start=1700000000,
    end=1800000000,
    attributes=True,
    scores=True,
)
blocked = query.follow_edges("blocks", name="blocked")
visible = connected.subtract(blocked, name="visible", degrees=True)

result = await query.execute()

visible_ids = result.get_nodes("visible")
connected_scores = result.get_scores("connected")
bob = result.nodes["bob"]
```

## Set Operations

Set operations are stages too, so you can keep traversing from them.

```python
query = nutmeg.query("viewer")

friends = query.follow_edges("connected_to", name="friends")
teammates = query.follow_edges("teammate_of", name="teammates")
blocked = query.follow_edges("blocks", name="blocked")

network = friends.union(teammates, name="network")
visible_network = network.subtract(blocked, name="visible_network")
mutuals = friends.intersect(teammates, name="mutuals")
only_one_group = friends.symmetric_difference(teammates, name="only_one_group")

posts = visible_network.follow_edges("posted", name="posts", attributes=True)

result = await query.execute()
```

Set operations are binary. Chain them when you need more than two inputs:
`a.union(b).union(c)`. Ordering is defined by the call:

- `a.union(b)` keeps `a` order, then appends `b` nodes not already present
- `a.intersect(b)` keeps `a` order for nodes also present in `b`
- `a.subtract(b)` keeps `a` order after removing `b`
- `a.symmetric_difference(b)` returns `a`-only in `a` order, then `b`-only in `b` order

Set-operation stages do not have scores; `scores=True` is only valid on traversal
stages created by `follow_edges()`.

## MCP

THe MCP server is kept intentionally light, it exposes the Graph's schema, get_node, and the query engine. 

Available tools:

| Tool | Args | Returns |
| --- | --- | --- |
| `get_node` | `node_id` | `{node_type, attributes, degree}` |
| `get_meta_graph` | -- | node, edge, node-edge, and node-edge-node counts |

Served over streamable HTTP at `http://127.0.0.1:3888`.

## Meta Graph

Alongside the graph itself, Nutmeg keeps live counts of how many nodes and
edges exist per type, so you can inspect a graph's shape without walking it.
`GET /meta` returns those counts in one snapshot. See
[docs/tech_details.md](docs/tech_details.md#meta-graph) for how it's stored
and kept consistent.

## Quickstart

See [docs/quickstart.md](docs/quickstart.md) for running Nutmeg with Docker or
locally.

## Bulk loading

High-throughput, non-transactional node and edge loading is available through
the Python client. See [docs/BULK_LOAD.md](docs/BULK_LOAD.md) for the
interface, batching behavior, partial-failure contract, and performance
guidance. Bulk loading is not exposed through MCP.

## API

| Method | Path | Body / Params |
| --- | --- | --- |
| `POST` | `/nodes` | `{node_id, node_type, attributes}` |
| `GET` | `/nodes/{node_id}` | -- |
| `DELETE` | `/nodes/{node_id}` | -- |
| `GET` | `/nodes/{node_id}/degree` | `?edge_type=` optional |
| `GET` | `/nodes/{node_id}/neighbors` | `?edge_types=&start=&end=` optional |
| `GET` | `/meta` | -- |
| `POST` | `/queries/execute` | serialized client query plan |
| `POST` | `/edges` | `{source_node, target_node, edge_type, attributes, score}` |
| `DELETE` | `/edges` | `?source_node=&target_node=&edge_type=` |

All writes are idempotent. A node's type cannot be changed after creation.
Interactive docs are at `/docs` once the server is running.

## Status Codes

Every error the API, MCP server, and Python client can raise carries a short,
stable `code` a caller can switch on, defined in `src/status_codes.py`. Where
useful, an error also carries a `reason` -- more detail specific to that code,
so the top-level list doesn't grow every time a new failure is distinguished.

| Code | Meaning | Reasons |
| --- | --- | --- |
| `OK` | Request succeeded. | -- |
| `SERVICE_UNAVAILABLE` | The graph store (Redis) could not be reached or timed out. Safe to retry. | -- |
| `NODE_NOT_FOUND` | A well-formed node id does not exist in the graph. | -- |
| `MAX_DEPTH_EXCEEDED` | A query plan chains more stages than the server allows to traverse (`MAX_QUERY_DEPTH` in `src/query_wire.py`). | -- |
| `INVALID_QUERY` | A request document is malformed. | e.g. `UNSUPPORTED_WIRE_VERSION`, `MISSING_START_NODES`, `MISSING_STAGES`, `DUPLICATE_STAGE_NAME`, `UNKNOWN_STAGE_KIND`, `INVALID_FOLLOW_STAGE`, `INVALID_SET_STAGE_SOURCES`, `MISSING_PARENT_STAGE`, `CYCLE`, `INVALID_IDENTIFIER`, `INVALID_BATCH_SIZE`, and others -- see `src/query_wire.py` and `src/graph.py` for the full set |
| `UNAUTHORIZED` | Reserved for future authentication support; nothing raises it yet. | -- |
| `BULK_PARTIAL_FAILURE` | A `/bulk-load` request completed, but some records were rejected. Not raised as an error -- it's the `code` field in an otherwise-200 bulk-load response when `errors` is non-empty. See [docs/BULK_LOAD.md](docs/BULK_LOAD.md). | -- |
| `RESOURCE_LIMIT_EXCEEDED` | A request exceeded a runtime resource limit. | `TIMEOUT` (server- or client-side), `RESULT_TOO_LARGE` (query touched more nodes than `max_result_nodes`) |

**API**: an error response is a JSON body `{"code", "detail", "reason"?}` with a
matching HTTP status (`NODE_NOT_FOUND` &rarr; 404, `INVALID_QUERY` /
`MAX_DEPTH_EXCEEDED` &rarr; 400, `UNAUTHORIZED` &rarr; 401,
`RESOURCE_LIMIT_EXCEEDED` &rarr; 429, `SERVICE_UNAVAILABLE` &rarr; 503).

**Python client**: `NutmegHTTPError` exposes `.code` and `.reason` alongside
the existing `.status_code` and `.detail`; both are `None` for a plain-text
error body. A client-side request timeout is also reported as
`RESOURCE_LIMIT_EXCEEDED` / `TIMEOUT`, the same as a server-side query timeout,
so callers can handle both the same way.

**MCP**: tool errors report `str(exc)`, which is prefixed `[CODE]` (or
`[CODE:REASON]`) so the code is recoverable even without a structured field.
