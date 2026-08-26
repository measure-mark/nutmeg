## Layout

All application code lives under `src/`; `main.py` and `mcp_main.py` at the repo
root are the two obvious entry points that run it.

- `main.py` -- run with `python main.py`.
- `mcp_main.py` -- run with `python mcp_main.py`.
- `src/graph.py` -- `NutmegGraph`, the async Redis-backed graph API.
- `src/bulk_redis.py` -- registered-script, non-transactional Redis bulk loading.
- `src/client.py` -- the async Python HTTP client and lazy query builder.
- `src/query_wire.py` -- the shared query wire format and validation used by both sides.
- `src/api/server.py` -- the FastAPI HTTP app.
- `src/api/query_engine.py` -- server-side execution for client query plans.
- `src/mcp_server/server.py` -- the FastMCP server exposing the graph.
- `tests/` -- pytest coverage for graph, API, MCP, client query building, query execution, and live integration.

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

## Bulk Load Performance and consistency

The server uses `redis.asyncio` registered Lua scripts inside pipelines created
with `transaction=False`. Registering the scripts lets Redis execute cached
scripts by digest instead of receiving the full Lua source for every record.
Pipelining reduces network round trips without wrapping the complete load in a
Redis transaction.

Each individual node or edge upsert remains atomic and maintains the same graph
indexes and metadata counters as `NutmegGraph.add_node` and
`NutmegGraph.add_edge`. The complete load is deliberately not atomic:

- completed batches remain committed if a later operation fails;
- other Redis clients may run commands between individual records;
- a type conflict or missing edge endpoint rejects that record but does not
  roll back successful records;
- nodes are always processed before edges in the same call.

The default batch size is 5,000 records and the maximum is 10,000. Smaller
batches use less memory for queued commands and replies; larger batches usually
reduce network overhead. Benchmark with representative records before changing
the default.

The entire call is currently one JSON HTTP request. For imports too large to fit
comfortably in application memory, call `bulk_load` repeatedly with source-level
chunks. Load all node chunks before edge chunks when edges can refer to nodes in
later chunks.