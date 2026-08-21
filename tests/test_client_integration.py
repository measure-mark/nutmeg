"""Live client test.

Skipped unless NUTMEG_LIVE_URL is set. Docker Compose wires that env var for the
client-integration service so this same test proves the client against Nutmeg + Redis.
"""

import json
import os
import time
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from src.client import Nutmeg, NutmegHTTPError


LIVE_URL = os.environ.get("NUTMEG_LIVE_URL")


def _request(method, path, body=None):
    url = f"{LIVE_URL.rstrip('/')}{path}"
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        raw = response.read()
    return None if not raw else json.loads(raw)


pytestmark = pytest.mark.skipif(not LIVE_URL, reason="set NUTMEG_LIVE_URL to run")


def test_client_query_against_live_api_and_redis():
    prefix = f"it_{uuid4().hex}"
    ada = f"{prefix}_ada"
    bob = f"{prefix}_bob"
    cara = f"{prefix}_cara"
    dana = f"{prefix}_dana"
    erin = f"{prefix}_erin"

    for attempt in range(20):
        try:
            _request("POST", "/nodes", {"node_id": ada, "node_type": "person"})
            break
        except HTTPError:
            raise
        except URLError:
            if attempt == 19:
                raise
            time.sleep(0.25)

    try:
        for node_id, name in [
            (bob, "Bob"),
            (cara, "Cara"),
            (dana, "Dana"),
            (erin, "Erin"),
        ]:
            _request(
                "POST",
                "/nodes",
                {
                    "node_id": node_id,
                    "node_type": "person",
                    "attributes": {"name": name},
                },
            )

        for source, target, edge_type, score in [
            (ada, bob, "connected_to", 1),
            (ada, cara, "connected_to", 2),
            (bob, dana, "connected_to", 1),
            (cara, dana, "connected_to", 1),
            (bob, erin, "blocks", 1),
        ]:
            _request(
                "POST",
                "/edges",
                {
                    "source_node": source,
                    "target_node": target,
                    "edge_type": edge_type,
                    "score": score,
                },
            )

        nutmeg = Nutmeg(LIVE_URL)
        query = nutmeg.query(ada)
        stage = query.follow_edges(
            "connected_to", name="stage", attributes=True, degrees=True
        )
        stage2 = stage.follow_edges("connected_to", name="stage2", degrees=True)
        stage2b = stage.follow_edges("blocks", name="stage2b")
        query.collapse("stage", "stage2", name="connections")

        result = query.execute()

        assert result.stages["stage"] == [bob, cara]
        assert result.stages["stage2"] == [dana]
        assert result.stages["stage2b"] == [erin]
        assert result.stages["connections"] == [bob, cara, dana]
        assert result.nodes[bob] == {
            "node_type": "person",
            "attributes": {"name": "Bob"},
            "degree": {"total": 2, "by_type": {"blocks": 1, "connected_to": 1}},
        }
        assert result.nodes[dana]["degree"] == {"total": 0, "by_type": {}}
        assert set(query.get_nodes("connections")).isdisjoint(query.get_nodes("stage2b"))

        with pytest.raises(NutmegHTTPError) as exc:
            nutmeg.get_node(f"{prefix}_nope")
        assert exc.value.status_code == 400
    finally:
        for node_id in [ada, bob, cara, dana, erin]:
            try:
                _request("DELETE", f"/nodes/{node_id}")
            except Exception:
                pass
