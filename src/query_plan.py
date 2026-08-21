"""Wire-format query plan helpers shared by the client and server."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


SET_KINDS = {"union", "intersect", "subtract", "symmetric_difference"}
ALLOWED_KINDS = {"start", "follow", *SET_KINDS}
ALLOWED_STAGE_FIELDS = {
    "name",
    "kind",
    "sources",
    "edge_type",
    "start",
    "end",
    "degrees",
    "attributes",
    "scores",
}


@dataclass(frozen=True)
class QueryStage:
    """One operation in a query plan.

    ``kind`` identifies the operation: ``start`` supplies initial node ids,
    ``follow`` traverses an edge type, and the set-operation kinds combine
    the outputs of two earlier stages.
    """

    name: str
    kind: str
    sources: tuple[str, ...] = ()
    edge_type: str | None = None
    start: float | None = None
    end: float | None = None
    degrees: bool = False
    attributes: bool = False
    scores: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.sources:
            data["sources"] = list(self.sources)
        if self.edge_type is not None:
            data["edge_type"] = self.edge_type
        if self.start is not None:
            data["start"] = self.start
        if self.end is not None:
            data["end"] = self.end
        if self.degrees:
            data["degrees"] = True
        if self.attributes:
            data["attributes"] = True
        if self.scores:
            data["scores"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryStage":
        if not isinstance(data, dict):
            raise ValueError("stage spec must be an object")
        unknown = set(data) - ALLOWED_STAGE_FIELDS
        if unknown:
            raise ValueError(f"Unknown stage fields: {sorted(unknown)}")
        for field in ("name", "kind"):
            if field not in data:
                raise ValueError(f"stage spec is missing required field {field!r}")
            if not isinstance(data[field], str):
                raise ValueError(f"stage field {field!r} must be a string")
        sources = data.get("sources", ())
        if not isinstance(sources, (list, tuple)) or not all(
            isinstance(source, str) for source in sources
        ):
            raise ValueError(f"stage {data['name']!r} sources must be a list of stage names")
        for field in ("degrees", "attributes", "scores"):
            if field in data and not isinstance(data[field], bool):
                raise ValueError(f"stage {data['name']!r} field {field!r} must be a boolean")
        return cls(
            name=data["name"],
            kind=data["kind"],
            sources=tuple(sources),
            edge_type=data.get("edge_type"),
            start=data.get("start"),
            end=data.get("end"),
            degrees=bool(data.get("degrees", False)),
            attributes=bool(data.get("attributes", False)),
            scores=bool(data.get("scores", False)),
        )


@dataclass(frozen=True)
class QueryPlan:
    start_nodes: list[str]
    stages: dict[str, QueryStage]


def load_query_plan(query_plan: dict[str, Any]) -> QueryPlan:
    if not isinstance(query_plan, dict):
        raise ValueError("query plan must be an object")
    if query_plan.get("wire_version") != 1:
        raise ValueError(f"Unsupported query wire_version {query_plan.get('wire_version')!r}")

    start_node_specs = query_plan.get("start_nodes", [])
    if not isinstance(start_node_specs, list) or not all(
        isinstance(node_id, str) for node_id in start_node_specs
    ):
        raise ValueError("query start_nodes must be a list of node ids")
    start_nodes = list(dict.fromkeys(start_node_specs))

    stage_specs = query_plan.get("stage_specs", [])
    if not isinstance(stage_specs, list):
        raise ValueError("query stage_specs must be a list")
    stages = [QueryStage.from_dict(spec) for spec in stage_specs]
    stage_map = validate_query_plan(start_nodes, stages)
    return QueryPlan(start_nodes=start_nodes, stages=stage_map)


def validate_query_plan(
    start_nodes: list[str],
    stages: Iterable[QueryStage] | dict[str, QueryStage],
) -> dict[str, QueryStage]:
    if not start_nodes:
        raise ValueError("query must contain at least one start node")

    stage_list = list(stages.values()) if isinstance(stages, dict) else list(stages)
    if not stage_list:
        raise ValueError("query must contain at least one stage")
    if len({stage.name for stage in stage_list}) != len(stage_list):
        raise ValueError("query contains duplicate stage names")

    for stage in stage_list:
        if stage.kind not in ALLOWED_KINDS:
            raise ValueError(f"Unknown stage kind {stage.kind!r}")
        if stage.kind == "start" and stage.sources:
            raise ValueError(f"start stage {stage.name!r} cannot have sources")
        if stage.kind != "follow" and stage.edge_type is not None:
            raise ValueError(f"{stage.kind} stage {stage.name!r} cannot have edge_type")
        if stage.kind != "follow" and (stage.start is not None or stage.end is not None):
            raise ValueError(f"{stage.kind} stage {stage.name!r} cannot have score bounds")
        if stage.kind == "follow" and (len(stage.sources) != 1 or not stage.edge_type):
            raise ValueError(f"follow stage {stage.name!r} requires one source and edge_type")
        if stage.kind in SET_KINDS and len(stage.sources) != 2:
            raise ValueError(f"{stage.kind} stage {stage.name!r} requires exactly two sources")
        if stage.kind != "follow" and stage.scores:
            raise ValueError(f"{stage.kind} stage {stage.name!r} cannot request scores")

    start_stages = [stage for stage in stage_list if stage.kind == "start"]
    if len(start_stages) != 1:
        raise ValueError("query must contain exactly one start stage")

    stage_map = {stage.name: stage for stage in stage_list}
    topological_stage_names(stage_map)
    return stage_map


def topological_stage_names(stages: dict[str, QueryStage]) -> list[str]:
    indegree = {name: 0 for name in stages}
    dependents = {name: [] for name in stages}

    for stage in stages.values():
        for source in stage.sources:
            if source not in stages:
                raise ValueError(f"Stage {stage.name!r} depends on missing stage {source!r}")
            indegree[stage.name] += 1
            dependents[source].append(stage.name)

    ready = deque(name for name in stages if indegree[name] == 0)
    ordered: list[str] = []
    while ready:
        name = ready.popleft()
        ordered.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(stages):
        raise ValueError("Query stages contain a cycle")
    return ordered
