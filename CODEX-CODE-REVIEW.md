# Codex code review

Reviewed 2026-08-14. Scope: the Redis-backed graph implementation, FastAPI
surface, Docker Compose deployment, and tests on `main` at `da8298c`.

## Executive summary

The happy-path graph behavior is small and readable, and the existing 20 tests
pass in the documented `nutmeg` environment. However, the storage format has two
data-integrity defects, the reverse index grows on nominally idempotent writes,
and the test suite never exercises the real database on which almost every
behavior depends.

`docker-compose.yml` **does reflect persistent Redis storage**: Redis is started
with AOF enabled (`--appendonly yes`) and `/data` is backed by the named
`redis-data` volume (`docker-compose.yml:18-26`). A live graph created through
the application image remained queryable after `docker compose restart redis`,
and Redis reported `aof_enabled:1`. The volume survives ordinary container
replacement and `docker compose down`; it is intentionally removed by
`docker compose down -v`. There is no backup/restore story, and the default AOF
fsync policy can still lose roughly the most recent second on a host crash.

## Findings

### 1. High until enforced: identifier delimiters can merge graphs or defeat cascades

`src/keys.py:12-17` concatenates user-controlled node IDs, edge types, and target
IDs with `:` without escaping or length-prefixing them. The Lua scripts duplicate
the same construction (`src/graph.py:34-36`, `49-51`, `86-90`, and `118-120`).
Consequently, distinct logical tuples can name one Redis key. For example:

```text
(source="a:b", edge_type="c")
(source="a",   edge_type="b:c")
```

Both map to `nutmeg:edges:a:b:c`. In a live Redis reproduction, two such
source/type pairs both returned both targets; deleting one adjacency list can
therefore delete or expose the other's edges.

The chosen domain rule is now that **node IDs must not contain `:`**. Node type is
stored separately, so canonical IDs should be values such as `BOS`, not values
that repeat the type such as `team:BOS`. This rule removes the colon-based key
ambiguity as long as it is enforced for every source and target ID. The current
code does not enforce it, and the README and tests still use colon-bearing IDs,
so the defect remains reachable until those are changed.

The reverse-index entry has a second ambiguous delimiter. `_ADD_EDGE_LUA` stores
`edge_type .. '|' .. source_node` (`src/graph.py:97`), while node deletion splits
on the first `|` (`src/graph.py:45-49`). A live edge with type `rel|bad` remained
after its target node was deleted because the source/type pair was decoded
incorrectly.

Enforce the node-ID rule in `NutmegGraph` itself so direct Python callers cannot
bypass it, and also validate it in the Pydantic request models so HTTP clients get
a clear 422 response. Apply validation to every method that accepts a node ID,
including both endpoints of `add_edge` and `delete_edge`, not only `add_node`.
Update the README examples and tests from values such as `player:1` and `team:BOS`
to typed nodes such as `1`/`player` and `BOS`/`team`. Existing persisted nodes with
colon-bearing IDs will need migration or explicit removal.

The `|` reverse-index ambiguity is independent of the colon rule. Either reserve
`|` in both node IDs and edge types, or replace the reverse-index entry with a
reversible encoding. Regression tests should assert that `:` is rejected in every
node-ID position, that node type remains separate from ID, and that `|` cannot
break target cascade deletion.

### 2. High: edges to absent nodes become dangling records that `delete_node` cannot clean

`add_edge` never checks that either endpoint exists (`src/graph.py:158-172`), but
`_DELETE_NODE_LUA` returns immediately when the node hash is absent
(`src/graph.py:27-30`). Thus the public API can create an edge to a never-created
target, after which deleting that target is a no-op and leaves the source's degree
and neighbors unchanged. This reproduced against live Redis: the source degree was
still 1 after `delete_node` on the absent target.

Choose and enforce one endpoint-lifecycle rule:

- If this is a graph of explicit nodes, atomically reject `add_edge` unless both
  node hashes exist and return a useful HTTP error.
- If dangling or implicit endpoints are supported, remove the early return and
  make deletion clean every index even when the node hash is absent.

Add live tests for missing source, missing target, deletion before node creation,
and edge creation racing with node deletion. The expected result should follow the
chosen contract.

### 3. Medium: edge upserts and deletes cause unbounded reverse-index growth

Every edge upsert executes `RPUSH` (`src/graph.py:97`), even when `ZADD` merely
updates the existing edge. `delete_edge` never removes that entry, and deleting a
source node does not remove its entries from target lists. The module explicitly
calls the index append-only (`src/graph.py:5-7`), but the README also promises that
all writes are idempotent. They are idempotent only in query results, not in stored
state or future deletion cost.

A live reproduction that upserted one edge three times produced an inbound list
length of 3. In a persistent service, update/delete cycles therefore consume memory
without bound and make target deletion repeatedly process the same stale tuple.

Store inbound tuples in an idempotent set (with unambiguous encoding), and remove
the tuple atomically in `delete_edge` and when deleting a source's outgoing edges.
Add a test that repeatedly upserts and deletes the same edge, then asserts both the
public graph result and the bounded internal reverse-index cardinality.

### 4. Medium: deletion work is unbounded inside a blocking Lua script

`_DELETE_NODE_LUA` reads every edge type, every outgoing neighbor, and every inbound
entry in one atomic script (`src/graph.py:32-56`). Redis cannot serve other commands
while that script is running. The append-only duplicate index makes the worst case
larger, but even a clean high-degree node can pause the entire database.

Define an expected maximum degree and test deletion latency/concurrent availability
at that size. If large degrees are valid, prefer a tombstone plus bounded batched
cleanup over one unbounded atomic traversal, while defining what readers observe
during cleanup.

### 5. Medium: the tests replace the primary dependency and contain false-positive assertions

Every graph and API test constructs `fakeredis.FakeStrictRedis`
(`tests/test_graph.py:14-159`, `tests/test_api.py:17-20`). This means the suite does
not test the target Redis version, Lua script loading/reloading, AOF persistence,
the Compose network/URL, or container restart behavior.

Two tests are particularly misleading:

- `test_delete_node_cascade_tolerates_stale_in_edge_entries` never creates the
  target node (`tests/test_graph.py:154-161`). The deletion script returns at its
  existence check, so the stale-entry loop named by the test never executes.
- `test_add_edge_is_idempotent_and_updates_score` only asserts degree remains 1
  (`tests/test_graph.py:41-47`); it never checks that the score or ordering changed.

Because `NutmegGraph` is primarily a Redis adapter, the real-Redis tests should be
the required suite. A small fake suite may remain for fast feedback, but it should
not be the acceptance gate.

### 6. Low: the Compose anchor creates an unintended third service

The reusable build anchor is declared as `services.app-base`
(`docker-compose.yml:1-13`), so Compose treats it as a runnable service.
`docker compose config --services` reports `redis`, `api`, and `app-base`; a normal
`docker compose up` therefore also starts a container with no application command.

Move the anchor to a top-level extension field such as `x-app-build: &app-build`,
then merge that extension into `services.api`. Add `docker compose config --services`
to a deployment smoke test and assert the intended service set.

## Recommended live test plan

The highest-value change is a dedicated integration fixture backed by an actual
Redis container. Use an isolated test database or isolated Compose project, and
clean only that test database. Then run the graph lifecycle tests against the live
client rather than `fakeredis`.

Prioritize these cases:

1. Create typed nodes; add multiple edge types and scores; verify degree, filtering,
   cross-type deduplication, and ordering; update an edge; delete an edge; delete a
   target and verify cascade cleanup.
2. Add the four regression families from findings 1-3: identifier validation and
   reverse-entry delimiters, endpoint existence, repeated upserts, and repeated
   add/delete cycles.
3. Exercise the FastAPI routes with `server.graph` backed by the live fixture so a
   single test covers request validation, Lua, Redis structures, and traversal.
4. Add an isolated Compose persistence test: write a graph through HTTP, recreate
   the Redis container while retaining its named volume, then read and mutate the
   same graph. Mutating after restart is important because it also checks redis-py's
   `NOSCRIPT` recovery for the registered Lua scripts.
5. Add concurrent writer/deleter tests that assert storage invariants after racing
   edge upserts, edge deletes, and node deletes.
6. Add a high-degree deletion test at the supported scale and observe concurrent
   `PING`/read latency.

For CI ergonomics, mark the container-recreation persistence test as slow, but keep
the ordinary live graph/API suite required on every change. The existing fake tests
run in 0.22 seconds, so replacing their fixture with a session-scoped real Redis
container should still keep the suite small while testing the actual product.

## Validation performed

- `conda run -n nutmeg pytest -q`: **20 passed**.
- `python -m compileall -q src api tests`: passed.
- `pip check` in the `nutmeg` environment: no broken requirements.
- `docker compose config`: Redis has AOF plus the `/data` named volume; it also
  exposed the unintended `app-base` service.
- Live `redis:8-alpine`: graph creation, scored traversal, and query after Redis
  restart succeeded.
- Live reproductions confirmed key aliasing, delimiter-broken cascade cleanup,
  absent-node dangling edges, and duplicate reverse-index entries.
