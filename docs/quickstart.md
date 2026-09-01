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
field, so an id should not repeat it. Ids are opaque -- `/`, `#`, `?` and spaces
are all fine, since the read routes take `node_id` as a query parameter rather
than a path segment. `:` is the one character an id may not contain; it is the
Redis key delimiter. Reads look like:

```
curl 'http://127.0.0.1:3879/nodes?node_id=ada'
curl 'http://127.0.0.1:3879/nodes/degree?node_id=ada'
curl 'http://127.0.0.1:3879/nodes/neighbors?node_id=ada&edge_types=plays_for'
```

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

`NutmegClient` traces too, under the service name `nutmeg-client` when it is the
process -- a notebook or a script -- and under the server's own name when it is
used inside one. It propagates trace context, so a client call, the API request it
makes, and the Redis commands beneath that are all one trace. Name the caller to
tell several of them apart:

```python
nutmeg = NutmegClient("http://127.0.0.1:3879", app_name="neighborhood-etl")
```

`app_name` lands on every client span as `nutmeg.app_name`. In a notebook, set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` before importing the client.
Spans are batched, so a short-lived script should call
`trace.get_tracer_provider().shutdown()` before exiting or the last ones never
leave the process.

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
