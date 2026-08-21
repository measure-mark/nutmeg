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


async def _request(method, path, body=None):
    url = f"{LIVE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.request(method, url, json=body)
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
                await _seed_node(node_id, "post" if name.startswith("post") else "person")

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
        assert exc.value.status_code == 400
    finally:
        for node_id in nodes.values():
            try:
                await _request("DELETE", f"/nodes/{node_id}")
            except Exception:
                pass
