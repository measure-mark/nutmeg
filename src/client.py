"""Small HTTP client and traversal query builder for Nutmeg."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _as_node_list(nodes: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(nodes, str):
        return [nodes]
    return list(nodes)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


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

    def get_neighbors(self, node_id: str, edge_types: list[str] | None = None) -> list[str]:
        params = {"edge_types": edge_types} if edge_types else None
        return self._request("GET", f"/nodes/{node_id}/neighbors", params=params)

    def query(
        self,
        start_nodes: str | list[str] | tuple[str, ...],
        *,
        name: str = "start_stage",
        degrees: bool = False,
        attributes: bool = False,
    ) -> "NutmegQuery":
        return NutmegQuery(self, start_nodes, name=name, degrees=degrees, attributes=attributes)

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


@dataclass(frozen=True)
class _StageSpec:
    name: str
    kind: str
    sources: tuple[str, ...] = ()
    edge_type: str | None = None
    degrees: bool = False
    attributes: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.sources:
            data["sources"] = list(self.sources)
        if self.edge_type is not None:
            data["edge_type"] = self.edge_type
        if self.degrees:
            data["degrees"] = True
        if self.attributes:
            data["attributes"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_StageSpec":
        return cls(
            name=data["name"],
            kind=data["kind"],
            sources=tuple(data.get("sources", ())),
            edge_type=data.get("edge_type"),
            degrees=bool(data.get("degrees", False)),
            attributes=bool(data.get("attributes", False)),
        )


class Stage:
    def __init__(self, query: "NutmegQuery", name: str):
        self.query = query
        self.name = name

    def follow_edges(
        self,
        edge_type: str,
        *,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query._add_follow_stage(
            self.name, edge_type, name=name, degrees=degrees, attributes=attributes
        )

    def follow_edge(
        self,
        edge_type: str,
        *,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.follow_edges(edge_type, name=name, degrees=degrees, attributes=attributes)

    def collapse(
        self,
        *stages: str | "Stage",
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> "Stage":
        return self.query.collapse(
            self, *stages, name=name, degrees=degrees, attributes=attributes
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
        self._stages: dict[str, _StageSpec] = {
            name: _StageSpec(name=name, kind="start", degrees=degrees, attributes=attributes)
        }
        self.start = Stage(self, name)
        self._result: QueryResult | None = None

    def follow_edges(
        self,
        edge_type: str,
        *,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> Stage:
        return self.start.follow_edges(
            edge_type, name=name, degrees=degrees, attributes=attributes
        )

    def follow_edge(
        self,
        edge_type: str,
        *,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> Stage:
        return self.follow_edges(edge_type, name=name, degrees=degrees, attributes=attributes)

    def collapse(
        self,
        *stages: str | Stage,
        name: str | None = None,
        degrees: bool = False,
        attributes: bool = False,
    ) -> Stage:
        stage_names = tuple(self._stage_name(stage) for stage in stages)
        stage_name = name or self._next_name("connections")
        self._add_stage(
            _StageSpec(
                name=stage_name,
                kind="collapse",
                sources=stage_names,
                degrees=degrees,
                attributes=attributes,
            )
        )
        return Stage(self, stage_name)

    def execute(self) -> "QueryResult":
        values_by_stage: dict[str, list[str]] = {}
        metadata_requests: dict[str, dict[str, bool]] = {}

        for stage_name in self.topological_stage_names():
            spec = self._stages[stage_name]
            if spec.kind == "start":
                values = self.start_nodes
            elif spec.kind == "follow":
                neighbors = (
                    self.client.get_neighbors(node_id, [spec.edge_type])
                    for source in spec.sources
                    for node_id in values_by_stage[source]
                )
                values = _unique([node for batch in neighbors for node in batch])
            elif spec.kind == "collapse":
                values = _unique(
                    [node for source in spec.sources for node in values_by_stage[source]]
                )
            else:
                raise ValueError(f"Unknown stage kind {spec.kind!r}")

            values_by_stage[stage_name] = values
            if spec.degrees or spec.attributes:
                for node_id in values:
                    request = metadata_requests.setdefault(
                        node_id, {"degrees": False, "attributes": False}
                    )
                    request["degrees"] = request["degrees"] or spec.degrees
                    request["attributes"] = request["attributes"] or spec.attributes

        nodes: dict[str, dict[str, Any]] = {}
        for node_id, flags in metadata_requests.items():
            node = self.client.get_node(node_id)
            compact = {"node_type": node["node_type"]}
            if flags["attributes"]:
                compact["attributes"] = node.get("attributes", {})
            if flags["degrees"]:
                compact["degree"] = node.get("degree")
            nodes[node_id] = compact

        self._result = QueryResult(stages=values_by_stage, nodes=nodes)
        return self._result

    def get_nodes(self, stage: str | Stage) -> list[str]:
        if self._result is None:
            raise RuntimeError("query has not been executed")
        return self._result.get_nodes(self._stage_name(stage))

    def topological_stage_names(self) -> list[str]:
        indegree = {name: 0 for name in self._stages}
        dependents = {name: [] for name in self._stages}

        for spec in self._stages.values():
            for source in spec.sources:
                if source not in self._stages:
                    raise ValueError(f"Stage {spec.name!r} depends on missing stage {source!r}")
                indegree[spec.name] += 1
                dependents[source].append(spec.name)

        ready = deque(name for name in self._stages if indegree[name] == 0)
        ordered: list[str] = []
        while ready:
            name = ready.popleft()
            ordered.append(name)
            for dependent in dependents[name]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if len(ordered) != len(self._stages):
            raise ValueError("Query stages contain a cycle")
        return ordered

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "start_nodes": self.start_nodes,
            "stage_specs": [spec.to_dict() for spec in self._stages.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, client: Nutmeg, data: dict[str, Any]) -> "NutmegQuery":
        if data.get("version", 1) != 1:
            raise ValueError(f"Unsupported query version {data.get('version')!r}")

        specs = [_StageSpec.from_dict(spec) for spec in data["stage_specs"]]
        if not specs:
            raise ValueError("query must contain at least one stage")
        if len({spec.name for spec in specs}) != len(specs):
            raise ValueError("query contains duplicate stage names")

        allowed_kinds = {"start", "follow", "collapse"}
        for spec in specs:
            if spec.kind not in allowed_kinds:
                raise ValueError(f"Unknown stage kind {spec.kind!r}")

        start_specs = [spec for spec in specs if spec.kind == "start"]
        if len(start_specs) != 1:
            raise ValueError("query must contain exactly one start stage")

        query = cls(client, data["start_nodes"], name=start_specs[0].name)
        query._stages = {spec.name: spec for spec in specs}
        query.start = Stage(query, start_specs[0].name)
        query.topological_stage_names()
        return query

    @classmethod
    def from_json(cls, client: Nutmeg, data: str) -> "NutmegQuery":
        return cls.from_dict(client, json.loads(data))

    def _add_follow_stage(
        self,
        source: str,
        edge_type: str,
        *,
        name: str | None,
        degrees: bool,
        attributes: bool,
    ) -> Stage:
        stage_name = name or self._next_name("stage")
        self._add_stage(
            _StageSpec(
                name=stage_name,
                kind="follow",
                sources=(source,),
                edge_type=edge_type,
                degrees=degrees,
                attributes=attributes,
            )
        )
        return Stage(self, stage_name)

    def _add_stage(self, spec: _StageSpec) -> None:
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
        return stage.name if isinstance(stage, Stage) else stage


@dataclass(frozen=True)
class QueryResult:
    stages: dict[str, list[str]]
    nodes: dict[str, dict[str, Any]]

    def get_nodes(self, stage: str) -> list[str]:
        return self.stages[stage]

    def to_dict(self) -> dict[str, Any]:
        return {"stages": self.stages, "nodes": self.nodes}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryResult":
        return cls(stages=data["stages"], nodes=data["nodes"])

    @classmethod
    def from_json(cls, data: str) -> "QueryResult":
        return cls.from_dict(json.loads(data))
