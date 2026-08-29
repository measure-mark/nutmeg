# Quickstart

## Docker

```
docker compose up -d
```

This starts Redis, the API, and the MCP server:

- API: `http://127.0.0.1:3879`
- MCP server: `http://127.0.0.1:3888`
- Redis: `127.0.0.1:6380` for host tools like `redis-cli`
- Traces: `http://localhost:4320` (otel-gui)
- Metrics: `http://localhost:8889/metrics` (Prometheus format)

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

## Observability

Both servers export OpenTelemetry traces and metrics, but only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set -- there is no separate on/off flag. Compose
sets it for you and points both services at a collector sidecar, which fans
traces out to the `otel-gui` viewer and exposes metrics for Prometheus.

Traces cover HTTP requests (FastAPI), MCP tool calls (FastMCP emits these
itself), and the Redis commands underneath both, so a single query's fan-out is
one trace. otel-gui keeps traces in memory only; they are gone on restart.

To trace a locally-run server against the Compose collector:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 OTEL_SERVICE_NAME=nutmeg-api \
  REDIS_URL=redis://localhost:6380/0 python main.py
```

Every other knob is a standard `OTEL_*` variable read by the SDK. The exporter
dependencies are optional (`pip install '.[otel]'`); without them nutmeg runs
normally as long as the endpoint variable is unset.

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
