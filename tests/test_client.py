"""Python client and traversal query behavior."""

import json
from urllib.error import HTTPError

import pytest

from src.client import Nutmeg, NutmegHTTPError, NutmegQuery, QueryResult


class FakeNutmeg(Nutmeg):
    def __init__(self):
        super().__init__("http://fake")
        self.neighbors = {
            ("ada", "connected_to"): ["bob", "cara"],
            ("bob", "connected_to"): ["dana"],
            ("cara", "connected_to"): ["dana", "erin"],
            ("dana", "connected_to"): ["frank"],
            ("erin", "connected_to"): ["frank"],
            ("bob", "blocks"): ["erin"],
            ("cara", "blocks"): ["dana"],
            ("dana", "knows"): ["grace"],
            ("erin", "knows"): ["heidi"],
            ("frank", "knows"): ["ivan"],
        }
        self.nodes = {
            "ada": {"node_type": "person", "attributes": {"name": "Ada"}, "degree": 1},
            "bob": {"node_type": "person", "attributes": {"name": "Bob"}, "degree": 2},
            "cara": {"node_type": "person", "attributes": {"name": "Cara"}, "degree": 2},
            "dana": {"node_type": "person", "attributes": {"name": "Dana"}, "degree": 1},
            "erin": {"node_type": "person", "attributes": {"name": "Erin"}, "degree": 1},
            "frank": {"node_type": "person", "attributes": {"name": "Frank"}, "degree": 1},
        }
        self.neighbor_calls = []
        self.node_calls = []

    def get_neighbors(self, node_id, edge_types=None):
        edge_type = edge_types[0] if edge_types else None
        self.neighbor_calls.append((node_id, edge_type))
        return self.neighbors.get((node_id, edge_type), [])

    def get_node(self, node_id):
        self.node_calls.append(node_id)
        return self.nodes[node_id]


class FakeHTTPBody:
    def __init__(self, body):
        self.body = body

    def read(self):
        if self.body is None:
            return b""
        if isinstance(self.body, str):
            return self.body.encode()
        return json.dumps(self.body).encode()

    def close(self):
        pass


class FakeHTTPResponse(FakeHTTPBody):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    calls = []
    responses = {}

    def fake(request, timeout):
        calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "timeout": timeout,
            }
        )
        response = responses[request.full_url]
        if isinstance(response, Exception):
            raise response
        return FakeHTTPResponse(response)

    monkeypatch.setattr("src.client.urlopen", fake)
    return responses, calls


def make_http_error(url, status_code, body):
    return HTTPError(url, status_code, "Bad Request", {}, FakeHTTPBody(body))


def build_branching_query(client):
    query = client.query("ada", attributes=True)
    stage = query.follow_edges("connected_to", name="stage", attributes=True)
    stage2 = stage.follow_edge("connected_to", name="stage2", degrees=True)
    stage2b = stage.follow_edges("blocks", name="stage2b")
    stage3 = stage2.follow_edge("connected_to", name="stage3")
    connections = query.collapse("stage", stage2, stage3, name="connections")
    connections.follow_edges("knows", name="visible_connections")
    return query, stage2b


def assert_dependencies_before_dependents(query):
    order = query.topological_stage_names()
    positions = {stage: index for index, stage in enumerate(order)}
    for spec in query.to_dict()["stage_specs"]:
        for source in spec.get("sources", []):
            assert positions[source] < positions[spec["name"]]


def test_http_client_serializes_requests_and_decodes_empty_body(fake_urlopen):
    responses, calls = fake_urlopen
    client = Nutmeg("http://nutmeg.test", timeout=3)
    responses.update(
        {
            "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b": ["bob"],
            "http://nutmeg.test/nodes/ada/degree": {
                "total": 1,
                "by_type": {"plays_for": 1},
            },
            "http://nutmeg.test/nodes/ada/degree?edge_type=plays_for": {
                "total": 1,
                "by_type": {"plays_for": 1},
            },
            "http://nutmeg.test/empty": None,
        }
    )

    assert client.get_neighbors("ada", ["a", "b"]) == ["bob"]
    assert client.get_degree("ada") == {"total": 1, "by_type": {"plays_for": 1}}
    assert client.get_degree("ada", "plays_for") == {
        "total": 1,
        "by_type": {"plays_for": 1},
    }
    assert client._request("DELETE", "/empty") is None

    assert calls == [
        {
            "method": "GET",
            "url": "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b",
            "headers": {},
            "timeout": 3,
        },
        {
            "method": "GET",
            "url": "http://nutmeg.test/nodes/ada/degree",
            "headers": {},
            "timeout": 3,
        },
        {
            "method": "GET",
            "url": "http://nutmeg.test/nodes/ada/degree?edge_type=plays_for",
            "headers": {},
            "timeout": 3,
        },
        {
            "method": "DELETE",
            "url": "http://nutmeg.test/empty",
            "headers": {},
            "timeout": 3,
        },
    ]


def test_http_client_unwraps_json_error_detail(fake_urlopen):
    responses, _ = fake_urlopen
    client = Nutmeg("http://nutmeg.test")
    url = "http://nutmeg.test/nodes/ghost"
    responses[url] = make_http_error(url, 400, {"detail": "node missing"})

    with pytest.raises(NutmegHTTPError) as exc:
        client.get_node("ghost")

    assert exc.value.status_code == 400
    assert exc.value.detail == "node missing"


def test_http_client_preserves_non_json_error_body(fake_urlopen):
    responses, _ = fake_urlopen
    client = Nutmeg("http://nutmeg.test")
    url = "http://nutmeg.test/nodes/ghost"
    responses[url] = make_http_error(url, 400, "plain bad")

    with pytest.raises(NutmegHTTPError) as exc:
        client.get_node("ghost")

    assert exc.value.status_code == 400
    assert exc.value.detail == "plain bad"


def test_topological_sort_handles_branching_and_collapse():
    query, _ = build_branching_query(FakeNutmeg())

    assert query.topological_stage_names()[0] == "start_stage"
    assert_dependencies_before_dependents(query)


def test_execute_returns_compact_stage_and_node_payload():
    client = FakeNutmeg()
    query, stage2b = build_branching_query(client)

    result = query.execute()

    assert result.to_dict() == {
        "stages": {
            "start_stage": ["ada"],
            "stage": ["bob", "cara"],
            "stage2": ["dana", "erin"],
            "stage2b": ["erin", "dana"],
            "stage3": ["frank"],
            "connections": ["bob", "cara", "dana", "erin", "frank"],
            "visible_connections": ["grace", "heidi", "ivan"],
        },
        "nodes": {
            "ada": {"node_type": "person", "attributes": {"name": "Ada"}},
            "bob": {"node_type": "person", "attributes": {"name": "Bob"}},
            "cara": {"node_type": "person", "attributes": {"name": "Cara"}},
            "dana": {"node_type": "person", "degree": 1},
            "erin": {"node_type": "person", "degree": 1},
        },
    }
    assert query.get_nodes("connections") == ["bob", "cara", "dana", "erin", "frank"]
    assert query.get_nodes(stage2b) == ["erin", "dana"]


def test_execute_uses_each_branch_source_not_only_the_latest_stage():
    client = FakeNutmeg()
    query = client.query("ada")
    stage = query.follow_edges("connected_to", name="stage")
    stage.follow_edges("connected_to", name="connected")
    stage.follow_edges("blocks", name="blocked")

    query.execute()

    assert ("bob", "connected_to") in client.neighbor_calls
    assert ("bob", "blocks") in client.neighbor_calls
    assert ("cara", "connected_to") in client.neighbor_calls
    assert ("cara", "blocks") in client.neighbor_calls


def test_query_roundtrips_through_dict_and_json():
    original, _ = build_branching_query(FakeNutmeg())

    from_dict = NutmegQuery.from_dict(FakeNutmeg(), original.to_dict())
    from_json = NutmegQuery.from_json(FakeNutmeg(), original.to_json())

    assert from_dict.to_dict() == original.to_dict()
    assert from_json.to_dict() == original.to_dict()
    assert from_json.execute().to_dict() == original.execute().to_dict()


def test_result_roundtrips_through_dict_and_json():
    result = QueryResult(
        stages={"start_stage": ["ada"], "stage": ["bob"]},
        nodes={"bob": {"node_type": "person", "attributes": {"name": "Bob"}}},
    )

    assert QueryResult.from_dict(result.to_dict()) == result
    assert QueryResult.from_json(result.to_json()) == result


def test_default_stage_names_are_stable_and_human_readable():
    query = FakeNutmeg().query("ada")
    query.follow_edges("connected_to")
    query.follow_edges("blocks")
    query.collapse("stage", "stage2")

    assert query.topological_stage_names() == [
        "start_stage",
        "stage",
        "stage2",
        "connections",
    ]


def test_duplicate_stage_name_is_rejected():
    query = FakeNutmeg().query("ada")
    query.follow_edges("connected_to", name="stage")

    with pytest.raises(ValueError, match="already exists"):
        query.follow_edges("blocks", name="stage")


def test_get_nodes_before_execute_is_rejected():
    query = FakeNutmeg().query("ada")

    with pytest.raises(RuntimeError, match="not been executed"):
        query.get_nodes("start_stage")


def test_metadata_requests_union_across_stages():
    client = FakeNutmeg()
    query = client.query("ada")
    stage = query.follow_edges("connected_to", name="stage")
    stage.follow_edges("connected_to", name="with_degrees", degrees=True)
    stage.follow_edges("connected_to", name="with_attributes", attributes=True)

    result = query.execute()

    assert result.nodes["dana"] == {
        "node_type": "person",
        "attributes": {"name": "Dana"},
        "degree": 1,
    }


def test_missing_dependency_is_rejected_when_loading_query():
    data = {
        "version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start"},
            {
                "name": "stage",
                "kind": "follow",
                "sources": ["missing"],
                "edge_type": "connected_to",
            }
        ],
    }

    with pytest.raises(ValueError, match="missing stage"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_cycle_is_rejected_when_loading_query():
    data = {
        "version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start"},
            {"name": "a", "kind": "collapse", "sources": ["b"]},
            {"name": "b", "kind": "collapse", "sources": ["a"]},
        ],
    }

    with pytest.raises(ValueError, match="cycle"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_duplicate_stage_names_are_rejected_when_loading_query():
    data = {
        "version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start"},
            {"name": "start_stage", "kind": "collapse", "sources": []},
        ],
    }

    with pytest.raises(ValueError, match="duplicate stage names"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_unknown_stage_kind_is_rejected_when_loading_query():
    data = {
        "version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start"},
            {"name": "typo", "kind": "folow", "sources": ["start_stage"]},
        ],
    }

    with pytest.raises(ValueError, match="Unknown stage kind"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_query_must_have_exactly_one_start_stage_when_loading_query():
    data = {
        "version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start"},
            {"name": "other_start", "kind": "start"},
        ],
    }

    with pytest.raises(ValueError, match="exactly one start"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_unknown_query_version_is_rejected_when_loading_query():
    data = {
        "version": 2,
        "start_nodes": ["ada"],
        "stage_specs": [{"name": "start_stage", "kind": "start"}],
    }

    with pytest.raises(ValueError, match="Unsupported query version"):
        NutmegQuery.from_dict(FakeNutmeg(), data)


def test_query_json_is_compact_valid_json():
    query = FakeNutmeg().query(["Ada Lovelace", "Ada Lovelace"])
    payload = query.to_json()

    assert json.dumps(json.loads(payload), separators=(",", ":"), sort_keys=True) == payload
    assert json.loads(payload)["start_nodes"] == ["Ada Lovelace"]
