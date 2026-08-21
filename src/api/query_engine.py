"""Server-side executor for Nutmeg client query plans."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from typing import Any

from src.graph import NutmegGraph
from src.query_plan import QueryStage, load_query_plan, topological_stage_names
from src.query_response import QueryResult, load_query_response


class QueryExecutor:
    def __init__(self, graph: NutmegGraph):
        self.graph = graph

    async def execute(self, query_plan: dict[str, Any]) -> dict[str, Any]:
        plan = load_query_plan(query_plan)
        stages = plan.stages
        start_nodes = plan.start_nodes

        values_by_stage: dict[str, list[str]] = {}
        scores_by_stage: dict[str, dict[str, float]] = {}
        metadata_requests: dict[str, dict[str, bool]] = {}

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
                    self._request_metadata(metadata_requests, values, stage)

        nodes = await self._collect_nodes(metadata_requests)
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

    def _request_metadata(
        self,
        metadata_requests: dict[str, dict[str, bool]],
        node_ids: list[str],
        stage: QueryStage,
    ) -> None:
        for node_id in node_ids:
            request = metadata_requests.setdefault(
                node_id, {"degrees": False, "attributes": False}
            )
            request["degrees"] = request["degrees"] or stage.degrees
            request["attributes"] = request["attributes"] or stage.attributes

    async def _collect_nodes(
        self, metadata_requests: dict[str, dict[str, bool]]
    ) -> dict[str, dict]:
        nodes: dict[str, dict] = {}
        node_ids = list(metadata_requests)
        pulled_nodes = await asyncio.gather(
            *(self.graph.get_node(node_id) for node_id in node_ids)
        )
        for node_id, node in zip(node_ids, pulled_nodes):
            flags = metadata_requests[node_id]
            compact = {"node_type": node["node_type"]}
            if flags["attributes"]:
                compact["attributes"] = node.get("attributes", {})
            if flags["degrees"]:
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
