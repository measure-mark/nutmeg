"""Live meta-graph tests against Redis, the HTTP API, and the MCP server."""

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as redis
from fastmcp import Client

from src import keys


LIVE_URL = os.environ.get("NUTMEG_LIVE_URL")
MCP_URL = os.environ.get("NUTMEG_MCP_URL")
REDIS_URL = os.environ.get("NUTMEG_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not all((LIVE_URL, MCP_URL, REDIS_URL)),
    reason="set NUTMEG_LIVE_URL, NUTMEG_MCP_URL, and NUTMEG_REDIS_URL to run",
)


async def _request(method, path, body=None):
    async with httpx.AsyncClient(base_url=LIVE_URL, timeout=5) as client:
        response = await client.request(method, path, json=body)
        response.raise_for_status()
        return response.json() if response.content else None


async def _add_node(node_id, node_type):
    await _request("POST", "/nodes", {"node_id": node_id, "node_type": node_type})


async def _add_edge(source, target, edge_type):
    await _request(
        "POST",
        "/edges",
        {"source_node": source, "target_node": target, "edge_type": edge_type},
    )


def _record(records, **fields):
    return next((record for record in records if all(record[key] == value for key, value in fields.items())), None)


async def test_meta_counts_follow_atomic_write_lifecycle():
    prefix = f"meta_{uuid4().hex}"
    person_type, team_type, league_type = (f"{prefix}_{name}" for name in ("person", "team", "league"))
    member_edge, self_edge = f"{prefix}_member", f"{prefix}_self"
    person1, person2, team, league = (f"{prefix}_{name}" for name in ("person1", "person2", "team1", "league1"))
    node_ids = [person1, person2, team, league]

    try:
        await asyncio.gather(*[_add_node(person1, person_type) for _ in range(12)])
        await asyncio.gather(_add_node(person2, person_type), _add_node(team, team_type), _add_node(league, league_type))

        async with httpx.AsyncClient(base_url=LIVE_URL, timeout=5) as client:
            conflict = await client.post("/nodes", json={"node_id": person1, "node_type": team_type})
            missing_target = await client.post(
                "/edges",
                json={"source_node": person1, "target_node": f"{prefix}_missing", "edge_type": f"{prefix}_invalid"},
            )
        assert conflict.status_code == 400
        assert missing_target.status_code == 400
        assert (await _request("GET", f"/nodes/{person1}"))["node_type"] == person_type

        await asyncio.gather(*[_add_edge(person1, team, member_edge) for _ in range(12)])
        await _add_edge(person2, team, member_edge)
        await _add_edge(team, league, member_edge)
        await _add_edge(person1, person1, self_edge)

        meta = await _request("GET", "/meta")
        expected_node_counts = {
            person_type: 2,
            team_type: 1,
            league_type: 1,
        }
        assert {node_type: meta["node_counts"][node_type] for node_type in expected_node_counts} == expected_node_counts
        assert meta["edge_counts"][member_edge] == 3
        assert meta["edge_counts"][self_edge] == 1
        assert _record(meta["node_edge_counts"], source_type=person_type, edge_type=member_edge)["count"] == 2
        assert _record(meta["node_edge_counts"], source_type=team_type, edge_type=member_edge)["count"] == 1
        assert _record(
            meta["node_edge_node_counts"],
            source_type=person_type,
            edge_type=member_edge,
            target_type=team_type,
        )["count"] == 2

        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        try:
            assert {key async for key in redis_client.scan_iter(match="nutmeg:meta:*")} == {
                keys.META_NODE_COUNTS,
                keys.META_EDGE_COUNTS,
                keys.META_NODE_EDGE_COUNTS,
                keys.META_NODE_EDGE_NODE_COUNTS,
            }
            assert await redis_client.hget(keys.META_NODE_COUNTS, person_type) == "2"
            assert await redis_client.hget(keys.META_EDGE_COUNTS, member_edge) == "3"
            assert await redis_client.hget(
                keys.META_NODE_EDGE_COUNTS, json.dumps([person_type, member_edge], separators=(",", ":"))
            ) == "2"
            assert await redis_client.hget(
                keys.META_NODE_EDGE_NODE_COUNTS,
                json.dumps([person_type, member_edge, team_type], separators=(",", ":")),
            ) == "2"
        finally:
            await redis_client.aclose()

        await asyncio.gather(
            *[
                _request(
                    "DELETE",
                    f"/edges?source_node={person2}&target_node={team}&edge_type={member_edge}",
                )
                for _ in range(12)
            ]
        )
        assert (await _request("GET", "/meta"))["edge_counts"][member_edge] == 2

        await _add_edge(person2, team, member_edge)
        await _request("DELETE", f"/nodes/{team}")
        meta = await _request("GET", "/meta")
        assert member_edge not in meta["edge_counts"]
        assert not any(record["edge_type"] == member_edge for record in meta["node_edge_counts"])
        assert not any(record["edge_type"] == member_edge for record in meta["node_edge_node_counts"])
        assert team_type not in meta["node_counts"]

        await _request("DELETE", f"/nodes/{person1}")
        meta = await _request("GET", "/meta")
        assert self_edge not in meta["edge_counts"]
        assert meta["node_counts"][person_type] == 1
    finally:
        for node_id in node_ids:
            try:
                await _request("DELETE", f"/nodes/{node_id}")
            except Exception:
                pass

    meta = await _request("GET", "/meta")
    assert not any(node_type.startswith(prefix) for node_type in meta["node_counts"])
    assert not any(edge_type.startswith(prefix) for edge_type in meta["edge_counts"])


async def test_mcp_meta_graph_matches_http_snapshot():
    prefix = f"mcp_meta_{uuid4().hex}"
    source, target = f"{prefix}_source", f"{prefix}_target"
    source_type, target_type, edge_type = f"{prefix}_source_type", f"{prefix}_target_type", f"{prefix}_edge"

    try:
        await _add_node(source, source_type)
        await _add_node(target, target_type)
        await _add_edge(source, target, edge_type)
        expected = await _request("GET", "/meta")

        async with Client(MCP_URL, timeout=10) as client:
            result = await client.call_tool("get_meta_graph", {})

        assert not result.is_error
        assert result.data == expected
    finally:
        for node_id in (source, target):
            try:
                await _request("DELETE", f"/nodes/{node_id}")
            except Exception:
                pass
