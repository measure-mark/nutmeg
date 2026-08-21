# Nutmeg

A simple typed graph database, backed by Redis, served over a small FastAPI HTTP layer.

Nodes and edges are typed. Edges are directed, optionally carry a score (e.g. a
timestamp, used for ordering) and free-form attributes. Out-edges live in Redis
sorted sets, keyed per `(node, edge_type)`, so a node's neighbors always come back
in score order.

See `CLAUDE.md` for the coding conventions this repo follows.

## Layout

All application code lives under `src/`; `main.py` and `mcp_main.py` at the repo
root are the two obvious entry points that run it.

- `main.py` — run with `python main.py`. Just a launcher: imports the FastAPI
  app and hands it to uvicorn.
- `mcp_main.py` — run with `python mcp_main.py`. Same pattern, for the MCP server.
- `src/graph.py` — `NutmegGraph`, the core API (`add_node`, `get_node`, `add_edge`,
  `get_degree`, `get_neighbors`, `delete_edge`, `delete_node`). Talks to Redis
  directly; no ORM, no in-memory layer yet.
- `src/client.py` — a small standard-library HTTP client plus a traversal query
  builder. Query plans are DAGs and execute in topological order.
- `src/keys.py` — the Redis key-naming scheme, in one place.
- `src/api/server.py` — a FastAPI app that exposes `NutmegGraph` over HTTP.
- `src/mcp_server/server.py` — an MCP server exposing the graph to model clients.
  Just `get_node()` for now -- enough to prove the infrastructure (container,
  port, transport) works end to end. Named `mcp_server`, not `mcp`, so it
  doesn't shadow the `mcp` SDK package this project also depends on.
- `tests/` — pytest coverage for the graph, API, MCP layer, client query DAG, and
  optional live client integration.

## Quickstart

### Docker (recommended)

```
docker compose up -d
```

This starts Redis, the API, and the MCP server:

- API: `http://127.0.0.1:3879`
- MCP server (streamable-http): `http://127.0.0.1:3888`
- Redis: `127.0.0.1:6380` for host tools like `redis-cli` (not 6379 -- nba's
  docker-compose already binds that port for its own Redis on this machine)

```
curl -X POST http://127.0.0.1:3879/nodes \
  -H 'Content-Type: application/json' \
  -d '{"node_id": "ada", "node_type": "player", "attributes": {"name": "Ada"}}'
```

Node ids are plain, globally-unique strings (e.g. `ada`, `celtics`) -- `node_type` is
already a separate field, so an id shouldn't repeat it (e.g. `player:ada`).

### Local (conda env)

```
conda env create -f environment.yml   # once
conda activate nutmeg
REDIS_URL=redis://localhost:6380/0 python main.py         # API on :3879
REDIS_URL=redis://localhost:6380/0 python mcp_main.py     # MCP server on :3888
```

For autoreload on the API during development, run uvicorn directly instead --
reload needs an import-string target, which `main.py` doesn't give it:

```
REDIS_URL=redis://localhost:6380/0 uvicorn src.api.server:app --reload --port 3879
```

`REDIS_URL` defaults to `redis://localhost:6379/0` if unset -- override it if your
Redis isn't on the default port (e.g. when nutmeg's Redis is running on 6380
alongside another project's Redis on 6379). `API_PORT`/`MCP_PORT` default to
`3879`/`3888` and only need overriding if those are taken too.

## Tests

```
conda activate nutmeg
pytest
```

Default tests do not require Docker or live Redis. Graph/API tests use
`fakeredis`, client HTTP tests use a tiny stdlib HTTP server, and the live
client integration test is skipped unless `NUTMEG_LIVE_URL` is set.

To prove the Python client against a live Nutmeg API backed by Redis:

```
docker compose up -d --build api
docker compose run --rm --build client-integration
```

That Compose service starts Redis and the API, seeds unique test data through
HTTP, runs `tests/test_client_integration.py`, and deletes the test nodes.

## API

| Method | Path | Body / Params |
| --- | --- | --- |
| `POST` | `/nodes` | `{node_id, node_type, attributes}` |
| `GET` | `/nodes/{node_id}` | -- |
| `DELETE` | `/nodes/{node_id}` | -- |
| `GET` | `/nodes/{node_id}/degree` | `?edge_type=` (optional) |
| `GET` | `/nodes/{node_id}/neighbors` | `?edge_types=` (repeatable, optional) |
| `POST` | `/edges` | `{source_node, target_node, edge_type, attributes, score}` |
| `DELETE` | `/edges` | `?source_node=&target_node=&edge_type=` |

All writes are idempotent. Interactive docs are at `/docs` once the server is running.

## Python Client

The client talks to the HTTP API and has no third-party runtime dependency.

```python
from src.client import Nutmeg

nutmeg = Nutmeg("http://127.0.0.1:3879")

node = nutmeg.get_node("ada")
degree = nutmeg.get_degree("ada")
plays_for_degree = nutmeg.get_degree("ada", "plays_for")
neighbors = nutmeg.get_neighbors("ada", ["connected_to"])
```

### Traversal Queries

Queries start from one node id or a list of node ids. Each `follow_edges()` call
creates a new stage from the stage it was called on; `follow_edge()` is an alias.
Stages can branch naturally because earlier stage handles stay usable. `collapse()`
unions earlier stage results, preserving first-seen order.

Python cannot see the variable name on the left side of an assignment, so pass
`name=` for stages you want to fetch by name later.

```python
from src.client import Nutmeg

nutmeg = Nutmeg("http://127.0.0.1:3879")

query = nutmeg.query("ada")
stage = query.follow_edges("connected_to", name="stage", attributes=True)
stage2 = stage.follow_edge("connected_to", name="stage2", degrees=True)
stage2b = stage.follow_edges("blocks", name="stage2b")
stage3 = stage2.follow_edge("connected_to", name="stage3")
connections = query.collapse("stage", "stage2", "stage3", name="connections")

# Collapsed stages are regular stages, so they can be followed too.
connections.follow_edges("connected_to", name="connections_next")

result = query.execute()

all_connections = query.get_nodes("connections")
blocked = query.get_nodes(stage2b)
connections_i_can_see = set(all_connections) - set(blocked)
```

If you omit `name=`, follow stages are named `stage`, `stage2`, `stage3`, and so
on. Collapses are named `connections`, `connections2`, and so on.

`degrees=True` and `attributes=True` are stage-local. Only nodes in stages that
request metadata are included in the `nodes` section. Node records always include
`node_type`; they include `attributes` and `degree` only when requested by at
least one stage containing that node.

The result payload is compact JSON:

```json
{
  "stages": {
    "start_stage": ["ada"],
    "stage": ["bob", "cara"],
    "stage2": ["dana"],
    "stage2b": ["erin"],
    "connections": ["bob", "cara", "dana"]
  },
  "nodes": {
    "bob": {"node_type": "person", "attributes": {"name": "Bob"}},
    "dana": {"node_type": "person", "degree": {"total": 0, "by_type": {}}}
  }
}
```

Query plans and results both round-trip cleanly:

```python
from src.client import QueryResult

saved_query = query.to_json()
query = nutmeg.query_from_json(saved_query)
saved_result = result.to_json()
result = QueryResult.from_json(saved_result)
```

Execution uses a topological sort over the query DAG. Loading a malformed plan
with a missing dependency or cycle raises `ValueError` before any HTTP traversal
calls are made.

The v1 query executor uses the existing per-node HTTP API: a follow stage makes
one neighbor request per node in its source stage, and metadata collection makes
one node request per node that needs attributes or degree information. That keeps
the client small, but large traversals will want future batch endpoints.

## MCP

One tool so far:

| Tool | Args | Returns |
| --- | --- | --- |
| `get_node` | `node_id` | `{node_type, attributes, degree}` |

Served over streamable-http, not stdio, so it's reachable as a network service
at `http://127.0.0.1:3888` rather than needing to be spawned as a local subprocess.
