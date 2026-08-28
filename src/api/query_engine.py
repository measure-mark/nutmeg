"""Server-side executor for Nutmeg client query plans."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from src.graph import NutmegGraph
from src.query_wire import QueryStage, load_query_wire, topological_stage_names
from src.query_response import QueryResult, load_query_response
from src.status_codes import ResourceLimitExceededError

# Defaults for QueryExecutor's two RESOURCE_LIMIT_EXCEEDED guards. Both are
# constructor overrides (not just module constants) so tests can exercise the
# limit without either mocking time or building a huge graph.
DEFAULT_QUERY_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RESULT_NODES = 5_000


@dataclass
class _NodeFields:
    """Which of a node's two optional response fields -- its stored `attributes`
    and its `degree` (out-edge counts) -- at least one stage asked to have
    attached to it. This is "metadata" in the sense used elsewhere in this
    module: everything about a node beyond its bare id, i.e. everything a stage
    can opt into via its `attributes`/`degrees` flags (see QueryStage and
    docs/mcp_server.md). A node can show up in several stages requesting
    different fields (e.g. one stage wants only attributes, another wants only
    degree) -- flags accumulate via _request_node_fields rather than being
    decided per-stage, since the response's `nodes` map has one entry per node,
    not one per (stage, node) pair."""

    want_attributes: bool = False
    want_degree: bool = False


class QueryExecutor:
    def __init__(
        self,
        graph: NutmegGraph,
        *,
        timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
        max_result_nodes: int = DEFAULT_MAX_RESULT_NODES,
    ):
        self.graph = graph
        self.timeout_seconds = timeout_seconds
        self.max_result_nodes = max_result_nodes

    async def execute(self, query_plan: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._execute(query_plan), self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ResourceLimitExceededError(
                f"query exceeded the {self.timeout_seconds}s time limit", reason="TIMEOUT"
            ) from exc

    async def _execute(self, query_plan: dict[str, Any]) -> dict[str, Any]:
        plan = load_query_wire(query_plan)
        stages = plan.stages
        start_nodes = plan.start_nodes

        values_by_stage: dict[str, list[str]] = {}
        scores_by_stage: dict[str, dict[str, float]] = {}
        # node_id -> which optional fields (see _NodeFields) to include for it in
        # the response's `nodes` map -- built up as stages execute, below.
        node_fields: dict[str, _NodeFields] = {}

        for level in self._topological_stage_levels(stages):
            tasks = {
                stage_name: asyncio.create_task(
                    self._execute_stage(stages[stage_name], start_nodes, values_by_stage)
                )
                for stage_name in level
            }
            for stage_name, task in tasks.items():
                values, score_map = await task
                stage = stages[stage_name]
                values_by_stage[stage_name] = values
                scores_by_stage[stage_name] = score_map
                if stage.degrees or stage.attributes:
                    self._request_node_fields(node_fields, values, stage)

        self._check_result_size(values_by_stage, scores_by_stage, stages, node_fields)
        nodes = await self._collect_nodes(node_fields)
        scores = {
            name: scores_by_stage[name]
            for name, stage in stages.items()
            if stage.scores
        }
        response = QueryResult(
            stages=values_by_stage,
            nodes=nodes,
            scores=scores,
        ).to_dict()
        return load_query_response(response, plan=plan).to_dict()

    def _check_result_size(
        self,
        values_by_stage: dict[str, list[str]],
        scores_by_stage: dict[str, dict[str, float]],
        stages: dict[str, QueryStage],
        node_fields: dict[str, _NodeFields],
    ) -> None:
        """Guards the response actually returned to the client: every entry in it.

        That means all three of the response's maps -- each stage's node list, each
        requested-scores stage's score map, and the `nodes` map of requested node
        documents. A node document is the largest entry of the three (attributes are
        arbitrary JSON), so leaving it out would let a plan requesting
        attributes/degrees return roughly twice the entries the limit implies.

        Runs before the _collect_nodes fan-out below, so an oversized plan is
        rejected before paying for per-node lookups whose result would only be
        thrown away -- node_fields is complete by now even though `nodes` isn't,
        since it has one key per node that will appear there.
        """
        stage_entries = sum(len(values) for values in values_by_stage.values())
        score_entries = sum(
            len(scores_by_stage[name]) for name, stage in stages.items() if stage.scores
        )
        total = stage_entries + score_entries + len(node_fields)
        if total > self.max_result_nodes:
            raise ResourceLimitExceededError(
                f"query result of {total} stage, score, and node-document entries "
                f"exceeds the limit of {self.max_result_nodes}",
                reason="RESULT_TOO_LARGE",
            )

    async def _execute_stage(
        self,
        stage: QueryStage,
        start_nodes: list[str],
        values_by_stage: dict[str, list[str]],
    ) -> tuple[list[str], dict[str, float]]:
        if stage.kind == "start":
            await self._validate_start_nodes(start_nodes)
            return start_nodes, {}
        if stage.kind == "follow":
            return await self._execute_follow(stage, values_by_stage)
        return self._execute_set_op(stage, values_by_stage)

    async def _validate_start_nodes(self, node_ids: list[str]) -> None:
        await asyncio.gather(
            *(self.graph.get_node(node_id) for node_id in node_ids)
        )

    async def _execute_follow(
        self,
        stage: QueryStage,
        values_by_stage: dict[str, list[str]],
    ) -> tuple[list[str], dict[str, float]]:
        candidates: dict[str, float] = {}
        source_nodes = values_by_stage[stage.sources[0]]
        edge_lists = await asyncio.gather(
            *(
                self.graph.get_neighbors(
                    source_node,
                    [stage.edge_type],
                    start=stage.start,
                    end=stage.end,
                    with_scores=True,
                )
                for source_node in source_nodes
            )
        )
        for edges in edge_lists:
            for edge in edges:
                node_id = edge["node_id"]
                score = edge["score"]
                if node_id not in candidates or score < candidates[node_id]:
                    candidates[node_id] = score

        ordered = sorted(candidates.items(), key=lambda kv: (kv[1], kv[0]))
        return [node_id for node_id, _ in ordered], dict(ordered)

    def _execute_set_op(
        self,
        stage: QueryStage,
        values_by_stage: dict[str, list[str]],
    ) -> tuple[list[str], dict[str, float]]:
        source_values = [values_by_stage[source] for source in stage.sources]
        source_sets = [set(values) for values in source_values]

        if stage.kind == "union":
            values = self._ordered_union(source_values)
        elif stage.kind == "intersect":
            values = [node for node in source_values[0] if all(node in s for s in source_sets[1:])]
        elif stage.kind == "subtract":
            blocked = set().union(*source_sets[1:])
            values = [node for node in source_values[0] if node not in blocked]
        elif stage.kind == "symmetric_difference":
            counts = Counter(node for values in source_values for node in set(values))
            values = []
            seen = set()
            for source in source_values:
                for node in source:
                    if counts[node] == 1 and node not in seen:
                        seen.add(node)
                        values.append(node)
        else:
            raise ValueError(f"Unknown stage kind {stage.kind!r}")

        return values, {}

    def _ordered_union(self, source_values: list[list[str]]) -> list[str]:
        seen = set()
        values = []
        for source in source_values:
            for node in source:
                if node not in seen:
                    seen.add(node)
                    values.append(node)
        return values

    def _request_node_fields(
        self,
        node_fields: dict[str, _NodeFields],
        node_ids: list[str],
        stage: QueryStage,
    ) -> None:
        for node_id in node_ids:
            fields = node_fields.setdefault(node_id, _NodeFields())
            fields.want_attributes = fields.want_attributes or stage.attributes
            fields.want_degree = fields.want_degree or stage.degrees

    async def _collect_nodes(self, node_fields: dict[str, _NodeFields]) -> dict[str, dict]:
        """Builds the response's `nodes` map: one NutmegGraph.get_node() call per
        node that at least one stage requested attributes and/or degree for (see
        _NodeFields), trimmed down to just the fields that node's requests
        actually asked for. A node's bare id already lives in `stages`; this is
        purely the opt-in extra detail about it."""
        nodes: dict[str, dict] = {}
        node_ids = list(node_fields)
        pulled_nodes = await asyncio.gather(
            *(self.graph.get_node(node_id) for node_id in node_ids)
        )
        for node_id, node in zip(node_ids, pulled_nodes):
            fields = node_fields[node_id]
            compact = {"node_type": node["node_type"]}
            if fields.want_attributes:
                compact["attributes"] = node.get("attributes", {})
            if fields.want_degree:
                compact["degree"] = node.get("degree")
            nodes[node_id] = compact
        return nodes

    def _topological_stage_levels(self, stages: dict[str, QueryStage]) -> list[list[str]]:
        topological_stage_names(stages)
        indegree = {name: 0 for name in stages}
        dependents = {name: [] for name in stages}
        for stage in stages.values():
            for source in stage.sources:
                indegree[stage.name] += 1
                dependents[source].append(stage.name)

        ready = deque(name for name in stages if indegree[name] == 0)
        levels: list[list[str]] = []
        while ready:
            level = list(ready)
            ready.clear()
            levels.append(level)
            for name in level:
                for dependent in dependents[name]:
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        ready.append(dependent)
        return levels
