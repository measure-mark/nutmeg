"""Shared query wire-format types and validation for the client and server."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from src.status_codes import InvalidQueryError, MaxDepthExceededError, NutmegError


def load_wire_json(
    data: str,
    *,
    error: type[NutmegError] = InvalidQueryError,
    reason: str = "INVALID_JSON",
) -> Any:
    """json.loads for a wire document supplied as text.

    A decode failure becomes a coded NutmegError rather than an uncoded
    JSONDecodeError: an entry point taking a document as a string owes callers
    the same code/reason contract as one taking it as a dict (see
    docs/status_codes.md). Which code depends on whose document it is -- a
    request the caller supplied (the INVALID_QUERY default) or a response a peer
    sent back (InvalidResponseError/INVALID_RESPONSE_JSON).
    """
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise error(f"could not parse JSON: {exc}", reason=reason) from exc


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

# Longest allowed chain of stage dependencies (a stage's own depth is
# 1 + max(source depths)). Guards against a plan whose sequential follow
# stages would force an unbounded number of Redis round trips.
MAX_QUERY_DEPTH = 11


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
            raise InvalidQueryError("stage spec must be an object", reason="INVALID_STAGE_SPEC")
        unknown = set(data) - ALLOWED_STAGE_FIELDS
        if unknown:
            raise InvalidQueryError(
                f"Unknown stage fields: {sorted(unknown)}", reason="UNKNOWN_STAGE_FIELDS"
            )
        for field in ("name", "kind"):
            if field not in data:
                raise InvalidQueryError(
                    f"stage spec is missing required field {field!r}",
                    reason="MISSING_STAGE_FIELD",
                )
            if not isinstance(data[field], str):
                raise InvalidQueryError(
                    f"stage field {field!r} must be a string", reason="INVALID_STAGE_FIELD"
                )
        sources = data.get("sources", ())
        if not isinstance(sources, (list, tuple)) or not all(
            isinstance(source, str) for source in sources
        ):
            raise InvalidQueryError(
                f"stage {data['name']!r} sources must be a list of stage names",
                reason="INVALID_STAGE_FIELD",
            )
        for field in ("degrees", "attributes", "scores"):
            if field in data and not isinstance(data[field], bool):
                raise InvalidQueryError(
                    f"stage {data['name']!r} field {field!r} must be a boolean",
                    reason="INVALID_STAGE_FIELD",
                )
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
class QueryWire:
    start_nodes: list[str]
    stages: dict[str, QueryStage]


def load_query_wire(query_plan: dict[str, Any]) -> QueryWire:
    if not isinstance(query_plan, dict):
        raise InvalidQueryError("query plan must be an object", reason="INVALID_PLAN")
    if query_plan.get("wire_version") != 1:
        raise InvalidQueryError(
            f"Unsupported query wire_version {query_plan.get('wire_version')!r}",
            reason="UNSUPPORTED_WIRE_VERSION",
        )

    start_node_specs = query_plan.get("start_nodes", [])
    if not isinstance(start_node_specs, list) or not all(
        isinstance(node_id, str) for node_id in start_node_specs
    ):
        raise InvalidQueryError(
            "query start_nodes must be a list of node ids", reason="INVALID_START_NODES"
        )
    start_nodes = list(dict.fromkeys(start_node_specs))

    stage_specs = query_plan.get("stage_specs", [])
    if not isinstance(stage_specs, list):
        raise InvalidQueryError("query stage_specs must be a list", reason="INVALID_STAGE_SPECS")
    stages = [QueryStage.from_dict(spec) for spec in stage_specs]
    stage_map = validate_query_wire(start_nodes, stages)
    return QueryWire(start_nodes=start_nodes, stages=stage_map)


def validate_query_wire(
    start_nodes: list[str],
    stages: Iterable[QueryStage] | dict[str, QueryStage],
) -> dict[str, QueryStage]:
    if not start_nodes:
        raise InvalidQueryError(
            "query must contain at least one start node", reason="MISSING_START_NODES"
        )

    stage_list = list(stages.values()) if isinstance(stages, dict) else list(stages)
    if not stage_list:
        raise InvalidQueryError("query must contain at least one stage", reason="MISSING_STAGES")
    if len({stage.name for stage in stage_list}) != len(stage_list):
        raise InvalidQueryError(
            "query contains duplicate stage names", reason="DUPLICATE_STAGE_NAME"
        )

    for stage in stage_list:
        if stage.kind not in ALLOWED_KINDS:
            raise InvalidQueryError(
                f"Unknown stage kind {stage.kind!r}", reason="UNKNOWN_STAGE_KIND"
            )
        if stage.kind == "start" and stage.sources:
            raise InvalidQueryError(
                f"start stage {stage.name!r} cannot have sources",
                reason="START_STAGE_HAS_SOURCES",
            )
        if stage.kind != "follow" and stage.edge_type is not None:
            raise InvalidQueryError(
                f"{stage.kind} stage {stage.name!r} cannot have edge_type",
                reason="UNEXPECTED_EDGE_TYPE",
            )
        if stage.kind != "follow" and (stage.start is not None or stage.end is not None):
            raise InvalidQueryError(
                f"{stage.kind} stage {stage.name!r} cannot have score bounds",
                reason="UNEXPECTED_SCORE_BOUNDS",
            )
        if stage.kind == "follow" and (len(stage.sources) != 1 or not stage.edge_type):
            raise InvalidQueryError(
                f"follow stage {stage.name!r} requires one source and edge_type",
                reason="INVALID_FOLLOW_STAGE",
            )
        if stage.kind in SET_KINDS and len(stage.sources) != 2:
            raise InvalidQueryError(
                f"{stage.kind} stage {stage.name!r} requires exactly two sources",
                reason="INVALID_SET_STAGE_SOURCES",
            )
        if stage.kind != "follow" and stage.scores:
            raise InvalidQueryError(
                f"{stage.kind} stage {stage.name!r} cannot request scores",
                reason="UNEXPECTED_SCORES",
            )

    start_stages = [stage for stage in stage_list if stage.kind == "start"]
    if len(start_stages) != 1:
        raise InvalidQueryError(
            "query must contain exactly one start stage", reason="INVALID_START_STAGE_COUNT"
        )

    stage_map = {stage.name: stage for stage in stage_list}
    topological_stage_names(stage_map)
    _check_max_depth(stage_map)
    return stage_map


def topological_stage_names(stages: dict[str, QueryStage]) -> list[str]:
    indegree = {name: 0 for name in stages}
    dependents = {name: [] for name in stages}

    for stage in stages.values():
        for source in stage.sources:
            if source not in stages:
                raise InvalidQueryError(
                    f"Stage {stage.name!r} depends on missing stage {source!r}",
                    reason="MISSING_PARENT_STAGE",
                )
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
        raise InvalidQueryError("Query stages contain a cycle", reason="CYCLE")
    return ordered


def _check_max_depth(stages: dict[str, QueryStage]) -> None:
    """Raise MaxDepthExceededError if any stage's dependency chain is too long.

    A standard iterative depth-first search over each stage's sources, 3-color
    marked (white/gray/black) so a cycle is caught by this pass alone rather than
    relying only on topological_stage_names's Kahn's-algorithm check above --
    belt and suspenders, since the two use unrelated algorithms. Iterative (an
    explicit stack, not Python call recursion) so a long, deliberately malicious
    stage chain fails with MaxDepthExceededError instead of RecursionError --
    unlike recursion, this doesn't care what order stages were declared in.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in stages}
    depth: dict[str, int] = {}

    for root in stages:
        if color[root] != WHITE:
            continue
        stack = [root]
        source_iters = {root: iter(stages[root].sources)}
        color[root] = GRAY
        while stack:
            name = stack[-1]
            source = next(source_iters[name], None)
            if source is None:
                depth[name] = 1 + max((depth[s] for s in stages[name].sources), default=-1)
                if depth[name] > MAX_QUERY_DEPTH:
                    raise MaxDepthExceededError(
                        f"query depth {depth[name]} exceeds the maximum of {MAX_QUERY_DEPTH}"
                    )
                color[name] = BLACK
                stack.pop()
                continue
            if color[source] == GRAY:
                raise InvalidQueryError("Query stages contain a cycle", reason="CYCLE")
            if color[source] == WHITE:
                color[source] = GRAY
                source_iters[source] = iter(stages[source].sources)
                stack.append(source)
