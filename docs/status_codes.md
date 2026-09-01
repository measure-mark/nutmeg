# Status Codes

Every error the API, MCP server, and Python client can raise carries a short,
stable `code` a caller can switch on, defined in `src/status_codes.py`. Where
useful, an error also carries a `reason` -- more detail specific to that code,
so the top-level list doesn't grow every time a new failure is distinguished.

Nutmeg is a client/server protocol with its own `code`/`reason` contract, not
a REST resource API -- `code` is the primary thing to switch on. HTTP status
(below) is secondary transport metadata layered on top of it, added only at
the FastAPI boundary.

The graph and bulk-load layers underneath raise plain Python exceptions with no
protocol awareness, so they stay usable in any context. `src/error_adapter.py`
is the one place that decides which code each of those carries; both public
surfaces go through it -- the API in `src/api/server.py`'s exception handlers,
the MCP server via its `@status_coded` tool decorator.

| Code | Meaning | Reasons |
| --- | --- | --- |
| `OK` | Request succeeded. | -- |
| `SERVICE_UNAVAILABLE` | The graph store (Redis) could not be reached or timed out. Safe to retry. Also how a bulk load interrupted mid-flight is reported, with the counts loaded before the interruption folded into `detail` -- see [BULK_LOAD.md](BULK_LOAD.md). | -- |
| `NODE_NOT_FOUND` | A well-formed node id does not exist in the graph. | -- |
| `MAX_DEPTH_EXCEEDED` | A query plan chains more stages than the server allows to traverse (`MAX_QUERY_DEPTH` in `src/query_wire.py`). | -- |
| `INVALID_QUERY` | A request document is malformed. | e.g. `UNSUPPORTED_WIRE_VERSION`, `MISSING_START_NODES`, `MISSING_STAGES`, `DUPLICATE_STAGE_NAME`, `UNKNOWN_STAGE_KIND`, `INVALID_FOLLOW_STAGE`, `INVALID_SET_STAGE_SOURCES`, `MISSING_PARENT_STAGE`, `CYCLE`, `INVALID_IDENTIFIER`, `INVALID_BATCH_SIZE`, `INVALID_REQUEST_DOCUMENT`, `INVALID_BULK_RECORD`, `NON_FINITE_NUMBER`, `FOREIGN_STAGE`, `INVALID_JSON`, and others -- see `src/query_wire.py`, `src/api/server.py`, and `src/client.py` for the full set |
| `UNAUTHORIZED` | Reserved for future authentication support; nothing raises it yet. | -- |
| `BULK_PARTIAL_FAILURE` | A `/bulk-load` request completed, but individual records were rejected. Not raised as an error -- it's the `code` field in an otherwise-200 bulk-load response when `errors` is non-empty. A Redis failure mid-load is *not* this: it stops the load and reports `SERVICE_UNAVAILABLE` instead. See [BULK_LOAD.md](BULK_LOAD.md). | -- |
| `RESOURCE_LIMIT_EXCEEDED` | A request exceeded a runtime resource limit. | `TIMEOUT` (server- or client-side), `RESULT_TOO_LARGE` (the query's combined stage and score entries exceeded `max_result_nodes`) |
| `INVALID_RESPONSE` | A protocol peer sent a response this end cannot accept. The one code that blames the sender rather than the caller's request. | `INVALID_RESPONSE_JSON` (the body didn't parse), `INVALID_RESPONSE_DOCUMENT` (it parsed but breaks the response contract in `src/query_response.py`) |
| `DATA_ERROR` | The graph rejected an otherwise well-formed write because of the data's own state -- a conflict with what's already stored, not a malformed request. | `NODE_TYPE_CONFLICT` (re-adding a node under a different, immutable type), and a generic (reason-less) fallback for any other data-layer rejection |

## Per-surface behavior

**API**: an error response is a JSON body `{"code", "detail", "reason"?}` with a
matching HTTP status:

| Code | HTTP status |
| --- | --- |
| `NODE_NOT_FOUND` | 404 |
| `INVALID_QUERY` / `MAX_DEPTH_EXCEEDED` | 400 |
| `UNAUTHORIZED` | 401 |
| `DATA_ERROR` | 409 |
| `RESOURCE_LIMIT_EXCEEDED` (reason `TIMEOUT`) | 504 |
| `RESOURCE_LIMIT_EXCEEDED` (reason `RESULT_TOO_LARGE`) | 413 |
| `RESOURCE_LIMIT_EXCEEDED` (other/future reason, e.g. `RATE_LIMITED`) | 429 |
| `SERVICE_UNAVAILABLE` | 503 |
| `INVALID_RESPONSE` | 500 |

`INVALID_RESPONSE` reaches the HTTP boundary only one way: `QueryExecutor`
validates the response it just assembled before returning it, so a failure there
means the server produced something invalid -- a server fault, hence 500. A
client receiving a bad response raises the same code locally, with no HTTP
status involved.

This includes requests FastAPI/Pydantic itself would otherwise reject with an
unstructured 422 (missing or wrongly typed fields) -- those are translated
into `INVALID_QUERY` / `INVALID_REQUEST_DOCUMENT` rather than FastAPI's
default body.

**Python client**: `NutmegHTTPError` exposes `.code` and `.reason` alongside
the existing `.status_code` and `.detail`; both are `None` for a plain-text
error body. A client-side request timeout is also reported as
`RESOURCE_LIMIT_EXCEEDED` / `TIMEOUT`, the same as a server-side query timeout,
so callers can handle both the same way.

Query-builder mistakes caught locally, before any request is sent -- an empty
start-node list, a duplicate or missing stage name, a set operation without
exactly two stages, a stage from another query -- raise `InvalidQueryError`
with the same reasons the server would use for the equivalent wire document
(`MISSING_START_NODES`, `DUPLICATE_STAGE_NAME`, `MISSING_PARENT_STAGE`,
`INVALID_SET_STAGE_SOURCES`) plus `FOREIGN_STAGE`, which has no wire
equivalent. Callers switch on `.code`/`.reason` whether validation happened
here or on the server.

Bulk-load records are checked locally the same way, before the request is built,
because a body of a hundred thousand records is no place to learn only that
*something* in it was bad. `INVALID_BULK_RECORD` covers a record that isn't a
mapping, is missing a required field, or gives a non-string id or type;
`NON_FINITE_NUMBER` covers a NaN or infinity in `attributes` or `score`, which
JSON cannot represent. Both name the record by kind and index and the field by
name -- `node at index 2 has node_id=NaN` -- since input this size is usually
machine-generated and the caller needs to find the row. NaN is called out
explicitly in the message: it is what a dataframe leaves behind for a missing
value, and by far the most common cause.

Anything else the JSON encoder rejects -- a numpy scalar, a set, a datetime --
is `INVALID_REQUEST_DOCUMENT`, raised for any route with a body. The client
encodes bodies itself rather than letting httpx do it, so these are coded
errors instead of a `ValueError` or `TypeError` from inside the transport.

The request-side entry points taking a wire document as text rather than a dict
-- `query_from_json` and `NutmegQuery.from_json` -- report unparseable input as
`INVALID_QUERY` / `INVALID_JSON`, so a malformed string is coded the same way a
malformed object is rather than surfacing Python's `JSONDecodeError`.

A bad *response* is `INVALID_RESPONSE` rather than `INVALID_QUERY`, since
nothing about the caller's input was wrong: a 2xx body that isn't JSON, and
`QueryResult.from_json` on unparseable text, give reason
`INVALID_RESPONSE_JSON`; a response that parses but breaks the wire contract
gives `INVALID_RESPONSE_DOCUMENT`.

**MCP**: tool errors report `str(exc)`, which is prefixed `[CODE]` (or
`[CODE:REASON]`) so the code is recoverable even without a structured field.

## Terminology note

Query-wire `INVALID_QUERY` reasons use this codebase's own "stage" vocabulary
(`DUPLICATE_STAGE_NAME`, `MISSING_PARENT_STAGE`, ...) rather than a "step"-based
vocabulary considered earlier in this feature's design, to stay consistent with
`QueryStage`/`stage_specs` used everywhere else in the wire format and code.
