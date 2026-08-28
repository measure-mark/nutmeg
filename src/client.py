"""Small HTTP client and lazy traversal query builder for Nutmeg."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any
import httpx

from src.bulk_redis import DEFAULT_BATCH_SIZE
from src.status_codes import InvalidQueryError, InvalidResponseError
from src.query_wire import QueryStage, load_query_wire, load_wire_json
from src.query_response import QueryResult, load_query_response


def _as_node_list(nodes: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(nodes, str):
        return [nodes]
    return list(nodes)


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in params.items() if value is not None and value != []
    }


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
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_node(self, node_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/nodes/{node_id}")

    async def get_degree(
        self,
        node_id: str,
        edge_type: str | None = None,
    ):
        params = {"edge_type": edge_type} if edge_type is not None else None
        return await self._request("GET", f"/nodes/{node_id}/degree", params=params)

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
                "edge_types": edge_types,
                "start": start,
                "end": end,
            }
        )
        return await self._request(
            "GET",
            f"/nodes/{node_id}/neighbors",
            params=params or None,
        )

    async def bulk_load(
        self,
        nodes: Iterable[Mapping[str, Any]] = (),
        edges: Iterable[Mapping[str, Any]] = (),
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> dict[str, Any]:
        """Load nodes, then edges, using non-transactional Redis pipelines."""
        return await self._request(
            "POST",
            "/bulk-load",
            body={
                "nodes": list(nodes),
                "edges": list(edges),
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
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, params=params, json=body)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error_body = exc.response.json()
                detail = error_body.get("detail", exc.response.text)
                code = error_body.get("code")
                reason = error_body.get("reason")
            except (ValueError, AttributeError):
                detail, code, reason = exc.response.text, None, None
            raise NutmegHTTPError(exc.response.status_code, detail, code=code, reason=reason) from exc
        except httpx.TimeoutException as exc:
            # The request never got a response at all -- same RESOURCE_LIMIT_EXCEEDED
            # code the server would report for a query that timed out server-side,
            # so callers can handle both the same way.
            raise NutmegHTTPError(
                408, f"request to {url} timed out", code="RESOURCE_LIMIT_EXCEEDED", reason="TIMEOUT"
            ) from exc
        if not response.content:
            return None
        # A 2xx whose body isn't JSON is the server breaking the contract, not the
        # caller sending bad input -- coded INVALID_RESPONSE so a client can tell
        # the two apart without inspecting exception classes.
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
