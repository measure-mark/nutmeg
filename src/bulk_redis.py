"""Fast, non-transactional bulk writes for the Redis-backed graph."""

from collections.abc import Iterable, Mapping
from typing import Any

import redis.exceptions

from src.graph_writes import GraphWriteScripts
from src.status_codes import InvalidQueryError, StatusCode


DEFAULT_BATCH_SIZE = 5_000
MAX_BATCH_SIZE = 10_000


class BulkLoadInterruptedError(redis.exceptions.RedisError):
    """A Redis-level failure (lost connection, etc.) interrupted a load partway
    through a batch. Deliberately not caught and turned into a per-record
    `errors` entry: unlike a rejected record (bad type, missing node), we can't
    tell which -- if any -- of the in-flight batch's writes landed, so we stop
    rather than risk silently skipping over an ambiguous batch and reporting a
    misleadingly clean partial result.

    `nodes_loaded`/`edges_loaded`/`errors` on this exception are exactly the
    result of every batch that completed *before* the one that failed -- since
    load() processes batches strictly in order and stops here, that boundary is
    the caller's resume point. Every write is an idempotent upsert, so it's
    always safe to retry starting from (or even before) that point.
    """

    def __init__(self, cause: Exception, nodes_loaded: int, edges_loaded: int, errors: list):
        super().__init__(
            f"bulk load interrupted after nodes_loaded={nodes_loaded} "
            f"edges_loaded={edges_loaded}: {cause}"
        )
        self.nodes_loaded = nodes_loaded
        self.edges_loaded = edges_loaded
        self.errors = errors


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
                f"batch_size {batch_size} is outside the allowed range of 1 to {MAX_BATCH_SIZE}",
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
        try:
            for start in range(0, len(node_records), batch_size):
                await self._load_node_batch(
                    node_records[start : start + batch_size], start, result
                )
            for start in range(0, len(edge_records), batch_size):
                await self._load_edge_batch(
                    edge_records[start : start + batch_size], start, result
                )
        except redis.exceptions.RedisError as exc:
            # Stop immediately -- do not attempt further batches. See
            # BulkLoadInterruptedError's docstring for why.
            raise BulkLoadInterruptedError(
                exc, result["nodes_loaded"], result["edges_loaded"], result["errors"]
            ) from exc

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
