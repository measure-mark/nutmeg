"""Live client tests against Nutmeg API + Redis.

Skipped unless NUTMEG_LIVE_URL is set. Docker Compose wires that env var for the
test-client-integration service so these prove the client against live Nutmeg + Redis.
"""

import asyncio
import os
from uuid import uuid4

import httpx

import pytest

from src.client import NutmegClient, NutmegHTTPError


LIVE_URL = os.environ.get("NUTMEG_LIVE_URL")


async def _request(method, path, body=None, params=None):
    url = f"{LIVE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.request(method, url, json=body, params=params)
        response.raise_for_status()
        return response.json() if response.content else None


pytestmark = pytest.mark.skipif(not LIVE_URL, reason="set NUTMEG_LIVE_URL to run")


async def _seed_node(node_id, node_type="person"):
    await _request(
        "POST",
        "/nodes",
        {"node_id": node_id, "node_type": node_type, "attributes": {"name": node_id}},
    )


async def _seed_edge(source, target, edge_type, score):
    await _request(
        "POST",
        "/edges",
        {
            "source_node": source,
            "target_node": target,
            "edge_type": edge_type,
            "score": score,
        },
    )


async def test_client_query_against_live_api_and_redis():
    prefix = f"it_{uuid4().hex}"
    nodes = {
        name: f"{prefix}_{name}"
        for name in [
            "viewer",
            "alt_viewer",
            "alice",
            "bob",
            "cara",
            "dana",
            "erin",
            "post1",
            "post2",
            "lonely",
        ]
    }

    for attempt in range(20):
        try:
            await _seed_node(nodes["viewer"])
            break
        except httpx.HTTPStatusError:
            raise
        except httpx.HTTPError:
            if attempt == 19:
                raise
            await asyncio.sleep(0.25)

    try:
        for name, node_id in nodes.items():
            if name != "viewer":
                await _seed_node(
                    node_id, "post" if name.startswith("post") else "person"
                )

        for source, target, edge_type, score in [
            ("viewer", "alice", "connected_to", 10),
            ("viewer", "bob", "connected_to", 20),
            ("viewer", "cara", "connected_to", 30),
            ("viewer", "erin", "blocks", 5),
            ("alt_viewer", "dana", "connected_to", 1),
            ("alt_viewer", "bob", "connected_to", 15),
            ("alice", "post1", "posted", 100),
            ("bob", "post2", "posted", 200),
        ]:
            await _seed_edge(nodes[source], nodes[target], edge_type, score)

        nutmeg = NutmegClient(LIVE_URL)
        query = nutmeg.query([nodes["viewer"], nodes["alt_viewer"]])
        connected = query.follow_edges(
            "connected_to",
            name="connected",
            start=10,
            end=30,
            attributes=True,
            scores=True,
        )
        blocked = query.follow_edges("blocks", name="blocked")
        visible = connected.subtract(blocked, name="visible", degrees=True)
        combined = visible.union(blocked, name="combined")
        visible.intersect(combined, name="visible_again")
        visible.symmetric_difference(blocked, name="changed")
        combined.follow_edges("posted", name="posts", scores=True)

        result = await query.execute()

        assert result.stages["connected"] == [
            nodes["alice"],
            nodes["bob"],
            nodes["cara"],
        ]
        assert result.scores["connected"] == {
            nodes["alice"]: 10.0,
            nodes["bob"]: 15.0,
            nodes["cara"]: 30.0,
        }
        assert result.stages["blocked"] == [nodes["erin"]]
        assert result.stages["visible"] == [nodes["alice"], nodes["bob"], nodes["cara"]]
        assert result.stages["combined"] == [
            nodes["alice"],
            nodes["bob"],
            nodes["cara"],
            nodes["erin"],
        ]
        assert result.stages["visible_again"] == [
            nodes["alice"],
            nodes["bob"],
            nodes["cara"],
        ]
        assert result.stages["changed"] == [
            nodes["alice"],
            nodes["bob"],
            nodes["cara"],
            nodes["erin"],
        ]
        assert result.stages["posts"] == [nodes["post1"], nodes["post2"]]
        assert result.nodes[nodes["bob"]] == {
            "node_type": "person",
            "attributes": {"name": nodes["bob"]},
            "degree": {"total": 1, "by_type": {"posted": 1}},
        }

        with pytest.raises(NutmegHTTPError) as exc:
            await nutmeg.get_node(f"{prefix}_nope")
        assert exc.value.status_code == 404
        assert exc.value.code == "NODE_NOT_FOUND"
    finally:
        for node_id in nodes.values():
            try:
                await _request("DELETE", "/nodes", params={"node_id": node_id})
            except Exception:
                pass


async def test_client_bulk_load_against_live_api_and_redis_is_partially_committed():
    prefix = f"bulk_{uuid4().hex}"
    existing = f"{prefix}_existing"
    new = f"{prefix}_new"
    missing = f"{prefix}_missing"
    edge_type = f"{prefix}_knows"
    node_type = f"{prefix}_person"
    nutmeg = NutmegClient(LIVE_URL)

    try:
        await _seed_node(existing, node_type)
        result = await nutmeg.bulk_load(
            nodes=[
                {"node_id": existing, "node_type": "team"},
                {"node_id": new, "node_type": node_type, "attributes": {"bulk": True}},
            ],
            edges=[
                {
                    "source_node": new,
                    "target_node": missing,
                    "edge_type": edge_type,
                },
                {
                    "source_node": existing,
                    "target_node": new,
                    "edge_type": edge_type,
                    "score": 42,
                },
            ],
            batch_size=1,
        )

        assert result == {
            "nodes_loaded": 1,
            "edges_loaded": 1,
            "errors": [
                {
                    "kind": "node",
                    "index": 0,
                    "message": f"node {existing!r} already has type {node_type!r}",
                },
                {
                    "kind": "edge",
                    "index": 0,
                    "message": f"target node {missing!r} does not exist",
                },
            ],
            "code": "BULK_PARTIAL_FAILURE",
        }
        assert (await nutmeg.get_node(existing))["node_type"] == node_type
        assert await nutmeg.get_node(new) == {
            "node_type": node_type,
            "attributes": {"bulk": True},
            "degree": {"total": 0, "by_type": {}},
        }
        assert await nutmeg.get_neighbors(existing, [edge_type]) == [new]
        meta = await _request("GET", "/meta")
        assert meta["node_counts"][node_type] == 2
        assert meta["edge_counts"][edge_type] == 1
    finally:
        for node_id in (existing, new):
            try:
                await _request("DELETE", "/nodes", params={"node_id": node_id})
            except Exception:
                pass


async def test_node_ids_containing_reserved_url_characters_round_trip():
    """The reason node routes take node_id as a query parameter rather than a path
    segment. A '/' in a path segment cannot survive routing -- ASGI percent-decodes
    before matching, so /nodes/a%2Fb arrives as two segments and 404s -- which would
    make any node whose id contains one permanently unreadable. Needs a live server:
    the decoding happens in uvicorn/starlette, so a fake transport cannot show it."""
    prefix = f"url_{uuid4().hex}"
    ids = [f"{prefix}/slash", f"{prefix}#hash", f"{prefix}?query", f"{prefix} space"]
    edge_type = f"{prefix}/edge"
    nutmeg = NutmegClient(LIVE_URL)

    try:
        for node_id in ids:
            await nutmeg.add_node(node_id, "person", {"raw": node_id})
        await nutmeg.add_edge(ids[0], ids[1], edge_type)

        for node_id in ids:
            node = await nutmeg.get_node(node_id)
            assert node["attributes"]["raw"] == node_id, node_id

        assert await nutmeg.get_neighbors(ids[0], [edge_type]) == [ids[1]]
        assert await nutmeg.get_degree(ids[0]) == {"total": 1, "by_type": {edge_type: 1}}

        await nutmeg.delete_edge(ids[0], ids[1], edge_type)
        assert await nutmeg.get_neighbors(ids[0], [edge_type]) == []

        await nutmeg.delete_node(ids[0])
        with pytest.raises(NutmegHTTPError) as exc:
            await nutmeg.get_node(ids[0])
        assert exc.value.code == "NODE_NOT_FOUND"
    finally:
        for node_id in ids:
            try:
                await _request("DELETE", "/nodes", params={"node_id": node_id})
            except Exception:
                pass


async def test_client_single_item_writes_round_trip_against_live_api():
    """The per-item write methods are thin wrappers over routes whose request shape
    the unit tests can only assert against a fake. This pins them to the real server:
    the bodies are accepted, delete_edge's query params identify the right edge, and
    delete_node takes its edges with it."""
    prefix = f"writes_{uuid4().hex}"
    ada, grace = f"{prefix}_ada", f"{prefix}_grace"
    node_type, edge_type = f"{prefix}_person", f"{prefix}_knows"
    nutmeg = NutmegClient(LIVE_URL)

    try:
        await nutmeg.add_node(ada, node_type, {"name": "Ada"})
        await nutmeg.add_node(grace, node_type)
        await nutmeg.add_edge(ada, grace, edge_type, {"since": "1843"}, score=0.5)

        assert await nutmeg.get_node(ada) == {
            "node_type": node_type,
            "attributes": {"name": "Ada"},
            "degree": {"total": 1, "by_type": {edge_type: 1}},
        }
        # attributes and score default server-side when the client omits them.
        assert await nutmeg.get_node(grace) == {
            "node_type": node_type,
            "attributes": {},
            "degree": {"total": 0, "by_type": {}},
        }
        assert await nutmeg.get_neighbors(ada, [edge_type]) == [grace]
        assert (await nutmeg.get_meta_graph())["edge_counts"][edge_type] == 1

        await nutmeg.delete_edge(ada, grace, edge_type)
        assert await nutmeg.get_neighbors(ada, [edge_type]) == []
        assert await nutmeg.get_degree(ada) == {"total": 0, "by_type": {}}

        await nutmeg.delete_node(ada)
        with pytest.raises(NutmegHTTPError) as exc:
            await nutmeg.get_node(ada)
        assert exc.value.code == "NODE_NOT_FOUND"
    finally:
        for node_id in (ada, grace):
            try:
                await _request("DELETE", "/nodes", params={"node_id": node_id})
            except Exception:
                pass
