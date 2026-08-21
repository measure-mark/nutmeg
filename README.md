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
- `src/keys.py` — the Redis key-naming scheme, in one place.
- `src/api/server.py` — a FastAPI app that exposes `NutmegGraph` over HTTP.
- `src/mcp_server/server.py` — an MCP server exposing the graph to model clients.
  Just `get_node()` for now -- enough to prove the infrastructure (container,
  port, transport) works end to end. Named `mcp_server`, not `mcp`, so it
  doesn't shadow the `mcp` SDK package this project also depends on.
- `tests/` — pytest, against `fakeredis` (no real Redis needed to run the suite).

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

Tests run entirely against `fakeredis`; no Redis instance is required.

## API

| Method | Path | Body / Params |
| --- | --- | --- |
| `POST` | `/nodes` | `{node_id, node_type, attributes}` |
| `DELETE` | `/nodes/{node_id}` | -- |
| `GET` | `/nodes/{node_id}/degree` | `?edge_type=` (optional) |
| `GET` | `/nodes/{node_id}/neighbors` | `?edge_types=` (repeatable, optional) |
| `POST` | `/edges` | `{source_node, target_node, edge_type, attributes, score}` |
| `DELETE` | `/edges` | `?source_node=&target_node=&edge_type=` |

All writes are idempotent. Interactive docs are at `/docs` once the server is running.

## MCP

One tool so far:

| Tool | Args | Returns |
| --- | --- | --- |
| `get_node` | `node_id` | `{node_type, attributes, degree}` |

Served over streamable-http, not stdio, so it's reachable as a network service
at `http://127.0.0.1:3888` rather than needing to be spawned as a local subprocess.
