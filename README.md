# Nutmeg

Nutmeg is a graph database using Redis for persistence, optimized for set
operations where edges are strictly ordered (e.g., by date).

It is built for questions like “who can I see after subtracting blockers?” or
“show me the 10 most recent concerts at the House of Blues?” A query stage is
a set of node ids. You branch, union, intersect, subtract, and continue
traversing from those sets. Nutmeg intentionally returns compact stage results
instead of full paths.

## Python Client

The client talks to the HTTP API and has no third-party runtime dependency.

```python
from src.client import NutmegClient

nutmeg = NutmegClient("http://127.0.0.1:3879")

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

## Response Shape

The response keeps stage outputs compact:

```json
{
  "wire_version": 1,
  "stages": {
    "start_stage": ["ada"],
    "connected": ["bob", "cara"],
    "blocked": ["erin"],
    "visible": ["bob", "cara"]
  },
  "nodes": {
    "bob": {
      "node_type": "person",
      "attributes": {"name": "Bob"},
      "degree": {"total": 1, "by_type": {"posted": 1}}
    }
  },
  "scores": {
    "connected": {"bob": 10.0, "cara": 20.0}
  }
}
```

`stages` are always lists of node ids. `nodes` contains only metadata requested
by stages with `attributes=True` or `degrees=True`. `scores` is present only for
traversal stages with `scores=True`; set-operation stages never emit scores.

Query plans and results round-trip cleanly:

```python
from src.client import QueryResult

saved_query = query.to_json()
query = nutmeg.query_from_json(saved_query)

saved_result = result.to_json()
result = QueryResult.from_json(saved_result)
```

# Tech details

## How Edges Are Stored

Nodes are Redis hashes keyed by node id. Edges are directed and typed. Each
out-edge set is a Redis sorted set keyed by `(source_node, edge_type)`, with the
target node id as the member and the edge score as the sorted-set score.

That sorted-set layout gives Nutmeg its query shape:

- neighbors come back in score order
- `start` and `end` are inclusive score bounds
- duplicate targets reached through multiple sources keep their best, lowest score

## Meta Graph

Nutmeg maintains node-type, edge-type, `(source_type, edge_type)`, and
`(source_type, edge_type, target_type)` counts in four Redis hashes. The same
Lua scripts that write nodes and edges update these counters, so graph data and
its metadata change atomically. `GET /meta` returns all four views in one
consistent snapshot.

Node types are immutable after creation. Re-adding a node with the same type
updates its attributes; re-adding it with a different type returns HTTP 400.

## Quickstart

### Docker

```
docker compose up -d
```

This starts Redis, the API, and the MCP server:

- API: `http://127.0.0.1:3879`
- MCP server: `http://127.0.0.1:3888`
- Redis: `127.0.0.1:6380` for host tools like `redis-cli`

```
curl -X POST http://127.0.0.1:3879/nodes \
  -H 'Content-Type: application/json' \
  -d '{"node_id": "ada", "node_type": "player", "attributes": {"name": "Ada"}}'
```

Node ids are plain, globally unique strings. `node_type` is already a separate
field, so an id should not repeat it.

### Local

```
conda env create -f environment.yml
conda activate nutmeg
REDIS_URL=redis://localhost:6380/0 python main.py
REDIS_URL=redis://localhost:6380/0 python mcp_main.py
```

For API autoreload:

```
REDIS_URL=redis://localhost:6380/0 uvicorn src.api.server:app --reload --port 3879
```

`REDIS_URL` defaults to `redis://localhost:6379/0`. `API_PORT` and `MCP_PORT`
default to `3879` and `3888`.

## Tests

```
conda activate nutmeg
pytest
```

Default tests do not require Docker or live Redis. Graph/API tests use
`fakeredis`, including a differential metadata recount after randomized mutation
traces; client HTTP tests use an async HTTP transport; query-engine contract
tests run against `NutmegGraph` directly.

To run the live Redis, HTTP API, Python client, and MCP integration tests:

```
docker compose run --rm --build test-client-integration
```

That Compose service starts Redis, the API, and MCP server, seeds unique test
data through HTTP, verifies the raw Redis metadata and both public surfaces,
and deletes the test nodes.

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

## MCP

Available tools:

| Tool | Args | Returns |
| --- | --- | --- |
| `get_node` | `node_id` | `{node_type, attributes, degree}` |
| `get_meta_graph` | -- | node, edge, node-edge, and node-edge-node counts |

Served over streamable HTTP at `http://127.0.0.1:3888`.

## Layout

All application code lives under `src/`; `main.py` and `mcp_main.py` at the repo
root are the two obvious entry points that run it.

- `main.py` -- run with `python main.py`.
- `mcp_main.py` -- run with `python mcp_main.py`.
- `src/graph.py` -- `NutmegGraph`, the async Redis-backed graph API.
- `src/client.py` -- the async Python HTTP client and lazy query builder.
- `src/query_wire.py` -- the shared query wire format and validation used by both sides.
- `src/api/server.py` -- the FastAPI HTTP app.
- `src/api/query_engine.py` -- server-side execution for client query plans.
- `src/mcp_server/server.py` -- the FastMCP server exposing the graph.
- `tests/` -- pytest coverage for graph, API, MCP, client query building, query execution, and live integration.
