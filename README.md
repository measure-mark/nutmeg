# Nutmeg

A simple typed graph database, backed by Redis, served over a small FastAPI HTTP layer.

Nodes and edges are typed. Edges are directed, optionally carry a score (e.g. a
timestamp, used for ordering) and free-form attributes. Out-edges live in Redis
sorted sets, keyed per `(node, edge_type)`, so a node's neighbors always come back
in score order.

See `CLAUDE.md` for the coding conventions this repo follows.

## Layout

- `src/graph.py` — `NutmegGraph`, the core API (`add_node`, `add_edge`, `get_degree`,
  `get_neighbors`, `delete_edge`, `delete_node`). Talks to Redis directly; no ORM,
  no in-memory layer yet.
- `src/keys.py` — the Redis key-naming scheme, in one place.
- `api/server.py` — a FastAPI app that exposes `NutmegGraph` over HTTP.
- `mcp/` — reserved for an MCP server exposing the graph to model clients (not built yet).
- `tests/` — pytest, against `fakeredis` (no real Redis needed to run the suite).

## Quickstart

### Docker (recommended)

```
docker compose up -d
```

This starts Redis and the API. The API is published on `http://127.0.0.1:8001`
(not 8000 -- see the port note in `docker-compose.yml` if you're wondering why).
Redis itself is published on `127.0.0.1:6380` for host tools like `redis-cli`.

```
curl -X POST http://127.0.0.1:8001/nodes \
  -H 'Content-Type: application/json' \
  -d '{"node_id": "ada", "node_type": "player", "attributes": {"name": "Ada"}}'
```

Node ids are plain, globally-unique strings (e.g. `ada`, `celtics`) -- `node_type` is
already a separate field, so an id shouldn't repeat it (e.g. `player:ada`).

### Local (conda env)

```
conda env create -f environment.yml   # once
conda activate nutmeg
REDIS_URL=redis://localhost:6380/0 uvicorn api.server:app --reload
```

`REDIS_URL` defaults to `redis://localhost:6379/0` if unset -- override it if your
Redis isn't on the default port (e.g. when nutmeg's Redis is running on 6380
alongside another project's Redis on 6379).

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
