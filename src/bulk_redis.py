"""Fast, non-transactional bulk writes for the Redis-backed graph."""

from collections.abc import Iterable, Mapping
from typing import Any

from src.graph import GraphWriteScripts
from src.status_codes import InvalidQueryError, StatusCode


DEFAULT_BATCH_SIZE = 5_000
MAX_BATCH_SIZE = 10_000


class BulkRedisLoader:
    """Pipeline registered graph-mutation scripts in bounded batches.

    A pipeline reduces network round trips but, with ``transaction=False``, does
    not make the complete load atomic. Each node or edge script is still atomic
    and retains the invariants of the corresponding ``NutmegGraph`` operation.
    """

    def __init__(self, redis_client):
        self._r = redis_client
        self._writes = GraphWriteScripts(redis_client)

    async def load(
        self,
        nodes: Iterable[Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> dict[str, Any]:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise InvalidQueryError(
                f"batch_size must be between 1 and {MAX_BATCH_SIZE}",
                reason="INVALID_BATCH_SIZE",
            )

        node_records = list(nodes)
        edge_records = list(edges)
        self._validate(node_records, edge_records)

        result: dict[str, Any] = {
            "nodes_loaded": 0,
            "edges_loaded": 0,
            "errors": [],
        }
        for start in range(0, len(node_records), batch_size):
            await self._load_node_batch(
                node_records[start : start + batch_size], start, result
            )
        for start in range(0, len(edge_records), batch_size):
            await self._load_edge_batch(
                edge_records[start : start + batch_size], start, result
            )
        result["code"] = (
            StatusCode.BULK_PARTIAL_FAILURE.value if result["errors"] else StatusCode.OK.value
        )
        return result

    def _validate(
        self,
        nodes: list[Mapping[str, Any]],
        edges: list[Mapping[str, Any]],
    ) -> None:
        for node in nodes:
            self._writes.validate_node(node["node_id"])
        for edge in edges:
            self._writes.validate_edge(
                edge["source_node"], edge["target_node"], edge["edge_type"]
            )

    async def _load_node_batch(
        self,
        records: list[Mapping[str, Any]],
        start: int,
        result: dict[str, Any],
    ) -> None:
        async with self._r.pipeline(transaction=False) as pipe:
            for node in records:
                await self._writes.add_node(
                    pipe,
                    node["node_id"],
                    node["node_type"],
                    node.get("attributes"),
                )
            responses = await pipe.execute()

        decoded = [
            (offset, self._writes.add_node_error(node["node_id"], response))
            for offset, (node, response) in enumerate(zip(records, responses))
        ]
        result["errors"].extend(
            {
                "kind": "node",
                "index": start + offset,
                "message": error,
            }
            for offset, error in decoded
            if error is not None
        )
        result["nodes_loaded"] += sum(error is None for _, error in decoded)

    async def _load_edge_batch(
        self,
        records: list[Mapping[str, Any]],
        start: int,
        result: dict[str, Any],
    ) -> None:
        async with self._r.pipeline(transaction=False) as pipe:
            for edge in records:
                await self._writes.add_edge(
                    pipe,
                    edge["source_node"],
                    edge["target_node"],
                    edge["edge_type"],
                    edge.get("attributes"),
                    edge.get("score", 0),
                )
            responses = await pipe.execute()

        decoded = [
            (
                offset,
                self._writes.add_edge_error(
                    edge["source_node"], edge["target_node"], response
                ),
            )
            for offset, (edge, response) in enumerate(zip(records, responses))
        ]
        result["errors"].extend(
            {
                "kind": "edge",
                "index": start + offset,
                "message": error,
            }
            for offset, error in decoded
            if error is not None
        )
        result["edges_loaded"] += sum(error is None for _, error in decoded)
