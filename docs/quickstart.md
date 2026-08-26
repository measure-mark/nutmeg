# Quickstart

## Docker

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

## Local

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
