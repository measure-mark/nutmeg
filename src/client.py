"""Small HTTP client and lazy traversal query builder for Nutmeg."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.query_plan import QueryStage, load_query_plan, topological_stage_names
from src.query_response import QueryResult, load_query_response


def _as_node_list(nodes: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(nodes, str):
        return [nodes]
    return list(nodes)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None and value != []}


class NutmegHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"Nutmeg HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class Nutmeg:
    def __init__(self, base_url: str = "http://127.0.0.1:3879", timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self._request("GET", f"/nodes/{node_id}")

    def get_degree(self, node_id: str, edge_type: str | None = None):
        params = {"edge_type": edge_type} if edge_type is not None else None
        return self._request("GET", f"/nodes/{node_id}/degree", params=params)

    def get_neighbors(
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
        return self._request("GET", f"/nodes/{node_id}/neighbors", params=params or None)

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

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ):
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"

        data = None if body is None else json.dumps(body).encode()
        request = Request(url, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            detail = raw.decode() if raw else ""
            try:
                detail = json.loads(detail).get("detail", detail)
            except json.JSONDecodeError:
                pass
            raise NutmegHTTPError(exc.code, detail) from exc

        if not raw:
            return None
        return json.loads(raw)


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

    def follow_edge(self, edge_type: str, **kwargs) -> "Stage":
        return self.follow_edges(edge_type, **kwargs)

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
        client: Nutmeg,
        start_nodes: str | list[str] | tuple[str, ...],
        *,
        name: str = "start_stage",
        degrees: bool = False,
        attributes: bool = False,
    ):
        self.client = client
        self.start_nodes = _unique(_as_node_list(start_nodes))
        if not self.start_nodes:
            raise ValueError("query must contain at least one start node")
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

    def follow_edge(self, edge_type: str, **kwargs) -> Stage:
        return self.follow_edges(edge_type, **kwargs)

    def execute(self) -> "QueryResult":
        payload = self.to_dict()
        plan = load_query_plan(payload)
        self._result = load_query_response(
            self.client._request("POST", "/queries/execute", body=payload),
            plan=plan,
        )
        return self._result

    def get_nodes(self, stage: str | Stage) -> list[str]:
        if self._result is None:
            raise RuntimeError("query has not been executed")
        return self._result.get_nodes(self._stage_name(stage))

    def get_scores(self, stage: str | Stage) -> dict[str, float]:
        if self._result is None:
            raise RuntimeError("query has not been executed")
        return self._result.get_scores(self._stage_name(stage))

    def topological_stage_names(self) -> list[str]:
        return topological_stage_names(self._stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wire_version": 1,
            "start_nodes": self.start_nodes,
            "stage_specs": [spec.to_dict() for spec in self._stages.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, client: Nutmeg, data: dict[str, Any]) -> "NutmegQuery":
        plan = load_query_plan(data)
        start_stage = next(stage for stage in plan.stages.values() if stage.kind == "start")
        query = cls(client, plan.start_nodes, name=start_stage.name)
        query._stages = plan.stages
        query.start = Stage(query, start_stage.name)
        return query

    @classmethod
    def from_json(cls, client: Nutmeg, data: str) -> "NutmegQuery":
        return cls.from_dict(client, json.loads(data))

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
            raise ValueError(f"{kind} requires exactly two stages")
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
            raise ValueError(f"Stage {spec.name!r} already exists")
        for source in spec.sources:
            if source not in self._stages:
                raise ValueError(f"Stage {spec.name!r} depends on missing stage {source!r}")
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
                raise ValueError(f"Stage {stage.name!r} belongs to a different query")
            return stage.name
        return stage
