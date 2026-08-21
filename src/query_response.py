"""Wire-format query response helpers shared by the client and server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.query_plan import QueryPlan


ALLOWED_RESPONSE_FIELDS = {"wire_version", "stages", "nodes", "scores"}
ALLOWED_NODE_FIELDS = {"node_type", "attributes", "degree"}


@dataclass(frozen=True)
class QueryResult:
    stages: dict[str, list[str]]
    nodes: dict[str, dict[str, Any]]
    scores: dict[str, dict[str, float]] = field(default_factory=dict)
    wire_version: int = 1

    def get_nodes(self, stage: str) -> list[str]:
        return self.stages[stage]

    def get_scores(self, stage: str) -> dict[str, float]:
        return self.scores.get(stage, {})

    def to_dict(self) -> dict[str, Any]:
        data = {
            "wire_version": self.wire_version,
            "stages": self.stages,
            "nodes": self.nodes,
        }
        if self.scores:
            data["scores"] = self.scores
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryResult":
        return load_query_response(data)

    @classmethod
    def from_json(cls, data: str) -> "QueryResult":
        return cls.from_dict(json.loads(data))


def load_query_response(
    response: dict[str, Any],
    *,
    plan: QueryPlan | None = None,
) -> QueryResult:
    if not isinstance(response, dict):
        raise ValueError("response must be an object")
    unknown = set(response) - ALLOWED_RESPONSE_FIELDS
    if unknown:
        raise ValueError(f"Unknown response fields: {sorted(unknown)}")
    if response.get("wire_version") != 1:
        raise ValueError(f"Unsupported response wire_version {response.get('wire_version')!r}")

    stages = _load_stages(response.get("stages"))
    nodes = _load_nodes(response.get("nodes"))
    scores = _load_scores(response.get("scores", {}), stages)

    if plan is not None:
        _validate_response_matches_plan(stages, scores, plan)

    return QueryResult(
        wire_version=1,
        stages=stages,
        nodes=nodes,
        scores=scores,
    )


def validate_query_response(response: dict[str, Any], *, plan: QueryPlan | None = None) -> None:
    load_query_response(response, plan=plan)


def _load_stages(data: Any) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        raise ValueError("response must contain stages")

    stages: dict[str, list[str]] = {}
    for stage_name, node_ids in data.items():
        if not isinstance(stage_name, str):
            raise ValueError("response stage names must be strings")
        if not isinstance(node_ids, list) or not all(isinstance(node, str) for node in node_ids):
            raise ValueError(f"response stage {stage_name!r} must be a list of node ids")
        stages[stage_name] = node_ids
    return stages


def _load_nodes(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("response must contain nodes")

    nodes: dict[str, dict[str, Any]] = {}
    for node_id, node in data.items():
        if not isinstance(node_id, str):
            raise ValueError("response node ids must be strings")
        if not isinstance(node, dict):
            raise ValueError(f"response node {node_id!r} must be an object")
        unknown = set(node) - ALLOWED_NODE_FIELDS
        if unknown:
            raise ValueError(f"response node {node_id!r} has unknown fields: {sorted(unknown)}")
        if not isinstance(node.get("node_type"), str):
            raise ValueError(f"response node {node_id!r} must contain node_type")
        if "attributes" in node and not isinstance(node["attributes"], dict):
            raise ValueError(f"response node {node_id!r} attributes must be an object")
        if "degree" in node and not isinstance(node["degree"], (int, dict)):
            raise ValueError(f"response node {node_id!r} degree must be a number or object")
        nodes[node_id] = node
    return nodes


def _load_scores(data: Any, stages: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    if not isinstance(data, dict):
        raise ValueError("response scores must be an object")

    scores: dict[str, dict[str, float]] = {}
    for stage_name, score_map in data.items():
        if stage_name not in stages:
            raise ValueError(f"response scores reference unknown stage {stage_name!r}")
        if not isinstance(score_map, dict):
            raise ValueError(f"response scores for stage {stage_name!r} must be an object")

        stage_nodes = set(stages[stage_name])
        scores[stage_name] = {}
        for node_id, score in score_map.items():
            if node_id not in stage_nodes:
                raise ValueError(
                    f"response score for stage {stage_name!r} references unknown node {node_id!r}"
                )
            if not isinstance(score, (int, float)):
                raise ValueError(
                    f"response score for stage {stage_name!r} node {node_id!r} must be numeric"
                )
            scores[stage_name][node_id] = float(score)
    return scores


def _validate_response_matches_plan(
    stages: dict[str, list[str]],
    scores: dict[str, dict[str, float]],
    plan: QueryPlan,
) -> None:
    expected_stages = set(plan.stages)
    actual_stages = set(stages)
    if actual_stages != expected_stages:
        raise ValueError(
            f"response stages {sorted(actual_stages)} do not match query stages {sorted(expected_stages)}"
        )

    expected_score_stages = {
        stage.name for stage in plan.stages.values() if stage.scores
    }
    actual_score_stages = set(scores)
    if actual_score_stages != expected_score_stages:
        raise ValueError(
            "response score stages "
            f"{sorted(actual_score_stages)} do not match requested score stages "
            f"{sorted(expected_score_stages)}"
        )
