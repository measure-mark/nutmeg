"""Small HTTP client and lazy traversal query builder for Nutmeg."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any
import httpx

from opentelemetry import propagate, trace

from src.bulk_redis import DEFAULT_BATCH_SIZE
from src.status_codes import InvalidQueryError, InvalidResponseError
from src.query_wire import QueryStage, load_query_wire, load_wire_json
from src.query_response import QueryResult, load_query_response
from src.telemetry import setup_telemetry

# What a client reports as when it is the process -- a notebook or a script rather
# than something running inside one of the servers.
CLIENT_SERVICE_NAME = "nutmeg-client"

# A ProxyTracer at import time: it resolves to whatever provider is installed later,
# so acquiring it here rather than per request is safe. With no SDK it is a no-op.
_TRACER = trace.get_tracer("nutmeg.client")


def _as_node_list(nodes: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(nodes, str):
        return [nodes]
    return list(nodes)


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in params.items() if value is not None and value != []
    }


_NODE_FIELDS = ("node_id", "node_type")
_EDGE_FIELDS = ("source_node", "target_node", "edge_type")

# Much of what gets bulk loaded comes out of a dataframe, where a missing value is
# a float NaN rather than an absent key -- so it is worth naming the likely cause.
_NAN_HINT = (
    " (a NaN here is usually a missing value in the source data -- drop or fill those"
    " rows before loading)"
)


def _describe(value: Any) -> str:
    """How an offending value should read back to the caller."""
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return f"{type(value).__name__} {value!r}"


def _encode_body(body: dict[str, Any]) -> bytes:
    """JSON-encode a request body, coding what the encoder rejects.

    Done here rather than left to httpx so a value JSON cannot represent -- a NaN,
    a numpy scalar, a set -- reaches the caller as a NutmegError they can switch on
    rather than a bare ValueError or TypeError raised inside the transport.
    """
    try:
        return json.dumps(body, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise InvalidQueryError(
            f"request body is not JSON-serializable: {exc}",
            reason="INVALID_REQUEST_DOCUMENT",
        ) from exc


def _check_json_numbers(value: Any, where: str, field: str) -> None:
    """Reject NaN and infinity, which have no JSON form, wherever they are nested."""
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidQueryError(
            f"{where} has {field}={_describe(value)}, which JSON cannot represent"
            + (_NAN_HINT if math.isnan(value) else ""),
            reason="NON_FINITE_NUMBER",
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_json_numbers(item, where, f"{field}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_json_numbers(item, where, f"{field}[{index}]")


def _check_bulk_records(
    records: list[Any], kind: str, required: tuple[str, ...]
) -> None:
    """Validate bulk-load records before sending, naming the one at fault.

    _encode_body would catch a bad value anyway, but only to say the body as a whole
    failed -- useless when the body holds a hundred thousand records. These checks
    cost one pass and buy a message that points at an index and a field.
    """
    for index, record in enumerate(records):
        where = f"{kind} at index {index}"
        if not isinstance(record, Mapping):
            raise InvalidQueryError(
                f"{where} must be a mapping of field to value, got {_describe(record)}",
                reason="INVALID_BULK_RECORD",
            )
        for field in required:
            if field not in record:
                raise InvalidQueryError(
                    f"{where} is missing required field {field!r}; "
                    f"every {kind} needs {', '.join(required)}",
                    reason="INVALID_BULK_RECORD",
                )
            value = record[field]
            if not isinstance(value, str):
                raise InvalidQueryError(
                    f"{where} has {field}={_describe(value)}, but it must be a string"
                    + (_NAN_HINT if isinstance(value, float) and math.isnan(value) else ""),
                    reason="INVALID_BULK_RECORD",
                )
        _check_json_numbers(record.get("attributes"), where, "attributes")
        _check_json_numbers(record.get("score"), where, "score")


class NutmegHTTPError(RuntimeError):
    """Raised for any non-2xx Nutmeg response.

    code/reason surface the server's status code (see docs/status_codes.md)
    when the body carries one; both are None for a plain-text error body or one
    predating that contract, so callers should not assume either is set.

    Query-builder mistakes caught before a request is ever sent raise
    InvalidQueryError instead -- same `code`/`reason` fields, no HTTP status.
    """

    def __init__(self, status_code: int, detail: Any, *, code: str | None = None, reason: str | None = None):
        super().__init__(f"Nutmeg HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.reason = reason


class NutmegClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3879",
        timeout: float = 10,
        app_name: str | None = None,
    ):
        """`app_name` names whatever is using the client -- a notebook, an ETL job,
        a service. It is attached to every span this client emits as
        `nutmeg.app_name`, so traces from several callers of the same Nutmeg can be
        told apart. Optional: leave it out and spans simply carry no such attribute.

        Telemetry is set up here rather than at import so a client used on its own
        reports as `nutmeg-client`. Inside a server the server's own name wins --
        the client is a library there, not the process. Either way this is a no-op
        unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.app_name = app_name
        setup_telemetry(CLIENT_SERVICE_NAME, fallback=True)

    async def add_node(
        self,
        node_id: str,
        node_type: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """One node at a time. Use bulk_load for more than a handful -- it pipelines,
        where this is a round trip per node."""
        await self._request(
            "POST",
            "/nodes",
            body={
                "node_id": node_id,
                "node_type": node_type,
                "attributes": dict(attributes or {}),
            },
        )

    async def add_edge(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
        attributes: Mapping[str, Any] | None = None,
        score: float = 0,
    ) -> None:
        await self._request(
            "POST",
            "/edges",
            body={
                "source_node": source_node,
                "target_node": target_node,
                "edge_type": edge_type,
                "attributes": dict(attributes or {}),
                "score": score,
            },
        )

    async def delete_node(self, node_id: str) -> None:
        """Deletes the node and every edge touching it, in either direction."""
        await self._request("DELETE", "/nodes", params={"node_id": node_id})

    async def delete_edge(
        self,
        source_node: str,
        target_node: str,
        edge_type: str,
    ) -> None:
        # Query params, not a body: DELETE /edges identifies the edge by its triple.
        await self._request(
            "DELETE",
            "/edges",
            params={
                "source_node": source_node,
                "target_node": target_node,
                "edge_type": edge_type,
            },
        )

    async def get_node(self, node_id: str) -> dict[str, Any]:
        return await self._request("GET", "/nodes", params={"node_id": node_id})

    async def get_degree(
        self,
        node_id: str,
        edge_type: str | None = None,
    ):
        params = _clean_params({"node_id": node_id, "edge_type": edge_type})
        return await self._request("GET", "/nodes/degree", params=params)

    async def get_neighbors(
        self,
        node_id: str,
        edge_types: list[str] | None = None,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> list[str]:
        params = _clean_params(
            {
                "node_id": node_id,
                "edge_types": edge_types,
                "start": start,
                "end": end,
            }
        )
        return await self._request("GET", "/nodes/neighbors", params=params)

    async def get_meta_graph(self) -> dict[str, Any]:
        """Node types, edge types, and how they connect -- the graph's shape."""
        return await self._request("GET", "/meta")

    async def bulk_load(
        self,
        nodes: Iterable[Mapping[str, Any]] = (),
        edges: Iterable[Mapping[str, Any]] = (),
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> dict[str, Any]:
        """Load nodes, then edges, using non-transactional Redis pipelines.

        One HTTP request, whatever the size of the input. All nodes are written
        before any edge, so an edge may reference a node loaded in the same call:

            result = await nutmeg.bulk_load(
                nodes=[
                    {"node_id": "ada", "node_type": "person",
                     "attributes": {"name": "Ada"}},
                    {"node_id": "grace", "node_type": "person"},
                ],
                edges=[
                    {"source_node": "ada", "target_node": "grace",
                     "edge_type": "knows", "score": 1,
                     "attributes": {"since": 1843}},
                ],
            )
            # {"nodes_loaded": 2, "edges_loaded": 1, "errors": [], "code": "OK"}

        `node_type` and the edge triple are required; `attributes` and `score`
        default to `{}` and `0`. Both arguments are accepted as any iterable and
        consumed once, so a generator works:

            await nutmeg.bulk_load(
                nodes=({"node_id": row.id, "node_type": "person"} for row in rows),
            )

        That is a convenience, not a memory strategy: the request is a single JSON
        body, so both iterables are materialized in full before it is sent. Split
        the input across several calls to bound peak memory.

        A rejected record -- a node whose type conflicts with what is stored, an
        edge whose endpoint does not exist -- does not fail the call. It comes
        back in `errors` by its zero-based index in the list you passed, and
        `code` is then `BULK_PARTIAL_FAILURE` rather than `OK`:

            result = await nutmeg.bulk_load(
                edges=[{"source_node": "ada", "target_node": "nobody",
                        "edge_type": "knows"}],
            )
            # {"nodes_loaded": 0, "edges_loaded": 0, "code": "BULK_PARTIAL_FAILURE",
            #  "errors": [{"kind": "edge", "index": 0, "message": "..."}]}

        Records are validated before anything is sent: a record that is not a
        mapping, is missing a required field, or carries a value JSON cannot
        represent (a NaN from a dataframe, most often) raises `InvalidQueryError`
        naming the record's index and field, and no request is made.

        Every write is an idempotent upsert, so fixing the input and resending is
        always safe. `batch_size` bounds one pipeline round trip (max 10,000);
        raise `timeout` on the client for big loads, since it covers the whole
        request. A lost Redis connection mid-load is the one case that raises
        instead: the server reports it as a 503, and the counts on the error mark
        the resume point. See docs/BULK_LOAD.md.
        """
        node_list, edge_list = list(nodes), list(edges)
        _check_bulk_records(node_list, "node", _NODE_FIELDS)
        _check_bulk_records(edge_list, "edge", _EDGE_FIELDS)
        return await self._request(
            "POST",
            "/bulk-load",
            body={
                "nodes": node_list,
                "edges": edge_list,
                "batch_size": batch_size,
            },
        )

    def query(
        self,
        start_nodes: str | list[str] | tuple[str, ...],
        *,
        name: str = "start_stage",
        degrees: bool = False,
        attributes: bool = False,
    ) -> "NutmegQuery":
        return NutmegQuery(
            self,
            start_nodes,
            name=name,
            degrees=degrees,
            attributes=attributes,
        )

    def query_from_dict(self, data: dict[str, Any]) -> "NutmegQuery":
        return NutmegQuery.from_dict(self, data)

    def query_from_json(self, data: str) -> "NutmegQuery":
        return NutmegQuery.from_json(self, data)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ):
        url = f"{self.base_url}{path}"
        content = None if body is None else _encode_body(body)
        headers = {} if content is None else {"content-type": "application/json"}
        # CLIENT span, and the one place trace context is injected: the traceparent
        # header is what lets the server's span for this request hang off this one,
        # so a single trace covers the caller, the API, and the Redis commands under
        # it. Exceptions leaving the block are recorded by the SDK, so the error
        # paths below need no telemetry of their own.
        with _TRACER.start_as_current_span(
            f"{method} {path}", kind=trace.SpanKind.CLIENT
        ) as span:
            if span.is_recording():
                span.set_attribute("http.request.method", method)
                span.set_attribute("url.full", url)
                if self.app_name is not None:
                    span.set_attribute("nutmeg.app_name", self.app_name)
            propagate.inject(headers)

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(
                        method, url, params=params, content=content, headers=headers
                    )
                    span.set_attribute("http.response.status_code", response.status_code)
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    error_body = exc.response.json()
                    detail = error_body.get("detail", exc.response.text)
                    code = error_body.get("code")
                    reason = error_body.get("reason")
                except (ValueError, AttributeError):
                    detail, code, reason = exc.response.text, None, None
                raise NutmegHTTPError(
                    exc.response.status_code, detail, code=code, reason=reason
                ) from exc
            except httpx.TimeoutException as exc:
                # The request never got a response at all -- same
                # RESOURCE_LIMIT_EXCEEDED code the server would report for a query
                # that timed out server-side, so callers handle both the same way.
                raise NutmegHTTPError(
                    408,
                    f"request to {url} timed out",
                    code="RESOURCE_LIMIT_EXCEEDED",
                    reason="TIMEOUT",
                ) from exc

            if not response.content:
                return None
            # A 2xx whose body isn't JSON is the server breaking the contract, not
            # the caller sending bad input -- coded INVALID_RESPONSE so a client can
            # tell the two apart without inspecting exception classes.
            return load_wire_json(
                response.text, error=InvalidResponseError, reason="INVALID_RESPONSE_JSON"
            )


class Stage:
    def __init__(self, query: "NutmegQuery", name: str):
        self.query = query
        self.name = name

    def follow_edges(
        self,
        edge_type: str,
        *,
        start: float | None = None,
        end: float | None = None,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
        scores: bool = False,
    ) -> "Stage":
        return self.query._add_follow_stage(
            self.name,
            edge_type,
            start=start,
            end=end,
            name=name,
            degrees=degrees,
            attributes=attributes,
            scores=scores,
        )

    def union(
        self,
        stage: str | "Stage",
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query._add_set_stage(
            "union",
            (self, stage),
            name=name,
            degrees=degrees,
            attributes=attributes,
        )

    def intersect(
        self,
        stage: str | "Stage",
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query._add_set_stage(
            "intersect",
            (self, stage),
            name=name,
            degrees=degrees,
            attributes=attributes,
        )

    def subtract(
        self,
        stage: str | "Stage",
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query._add_set_stage(
            "subtract",
            (self, stage),
            name=name,
            degrees=degrees,
            attributes=attributes,
        )

    def symmetric_difference(
        self,
        stage: str | "Stage",
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query._add_set_stage(
            "symmetric_difference",
            (self, stage),
            name=name,
            degrees=degrees,
            attributes=attributes,
        )


class NutmegQuery:
    def __init__(
        self,
        client: NutmegClient,
        start_nodes: str | list[str] | tuple[str, ...],
        *,
        name: str = "start_stage",
        degrees: bool = False,
        attributes: bool = False,
    ):
        self.client = client
        self.start_nodes = list(dict.fromkeys(_as_node_list(start_nodes)))
        if not self.start_nodes:
            raise InvalidQueryError(
                "query must contain at least one start node", reason="MISSING_START_NODES"
            )
        self._stages: dict[str, QueryStage] = {
            name: QueryStage(
                name=name,
                kind="start",
                degrees=degrees,
                attributes=attributes,
            )
        }
        self.start = Stage(self, name)
        self._result: QueryResult | None = None

    def follow_edges(self, edge_type: str, **kwargs) -> Stage:
        return self.start.follow_edges(edge_type, **kwargs)

    async def execute(self) -> "QueryResult":
        payload = self.to_dict()
        wire = load_query_wire(payload)
        response = await self.client._request("POST", "/queries/execute", body=payload)
        self._result = load_query_response(response, plan=wire)
        return self._result

    def get_nodes(self, stage: str | Stage) -> list[str]:
        if self._result is None:
            raise RuntimeError("query has not been executed")
        return self._result.get_nodes(self._stage_name(stage))

    def get_scores(self, stage: str | Stage) -> dict[str, float]:
        if self._result is None:
            raise RuntimeError("query has not been executed")
        return self._result.get_scores(self._stage_name(stage))

    def to_dict(self) -> dict[str, Any]:
        return {
            "wire_version": 1,
            "start_nodes": self.start_nodes,
            "stage_specs": [spec.to_dict() for spec in self._stages.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, client: NutmegClient, data: dict[str, Any]) -> "NutmegQuery":
        wire = load_query_wire(data)
        start_stage = next(
            stage for stage in wire.stages.values() if stage.kind == "start"
        )
        query = cls(client, wire.start_nodes, name=start_stage.name)
        query._stages = wire.stages
        query.start = Stage(query, start_stage.name)
        return query

    @classmethod
    def from_json(cls, client: NutmegClient, data: str) -> "NutmegQuery":
        return cls.from_dict(client, load_wire_json(data))

    def _add_follow_stage(
        self,
        source: str,
        edge_type: str,
        *,
        start: float | None,
        end: float | None,
        name: str | None,
        degrees: bool,
        attributes: bool,
        scores: bool,
    ) -> Stage:
        stage_name = name or self._next_name("stage")
        self._add_stage(
            QueryStage(
                name=stage_name,
                kind="follow",
                sources=(source,),
                edge_type=edge_type,
                start=start,
                end=end,
                degrees=degrees,
                attributes=attributes,
                scores=scores,
            )
        )
        return Stage(self, stage_name)

    def _add_set_stage(
        self,
        kind: str,
        stages: tuple[str | Stage, ...],
        *,
        name: str | None,
        degrees: bool,
        attributes: bool,
    ) -> Stage:
        stage_names = tuple(self._stage_name(stage) for stage in stages)
        if len(stage_names) != 2:
            raise InvalidQueryError(
                f"{kind} requires exactly two stages", reason="INVALID_SET_STAGE_SOURCES"
            )
        stage_name = name or self._next_name(kind)
        self._add_stage(
            QueryStage(
                name=stage_name,
                kind=kind,
                sources=stage_names,
                degrees=degrees,
                attributes=attributes,
            )
        )
        return Stage(self, stage_name)

    def _add_stage(self, spec: QueryStage) -> None:
        if spec.name in self._stages:
            raise InvalidQueryError(
                f"Stage {spec.name!r} already exists", reason="DUPLICATE_STAGE_NAME"
            )
        for source in spec.sources:
            if source not in self._stages:
                raise InvalidQueryError(
                    f"Stage {spec.name!r} depends on missing stage {source!r}",
                    reason="MISSING_PARENT_STAGE",
                )
        self._stages[spec.name] = spec

    def _next_name(self, base: str) -> str:
        if base not in self._stages:
            return base
        index = 2
        while f"{base}{index}" in self._stages:
            index += 1
        return f"{base}{index}"

    def _stage_name(self, stage: str | Stage) -> str:
        if isinstance(stage, Stage):
            if stage.query is not self:
                raise InvalidQueryError(
                    f"Stage {stage.name!r} belongs to a different query",
                    reason="FOREIGN_STAGE",
                )
            return stage.name
        return stage
