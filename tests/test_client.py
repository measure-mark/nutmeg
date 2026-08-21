"""Python client query construction and HTTP behavior."""

import json
from urllib.error import HTTPError

import pytest

from src.client import Nutmeg, NutmegHTTPError, NutmegQuery, QueryResult


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
                "body": None if request.data is None else json.loads(request.data),
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


def test_http_client_serializes_direct_requests_and_decodes_empty_body(fake_urlopen):
    responses, calls = fake_urlopen
    client = Nutmeg("http://nutmeg.test", timeout=3)
    responses.update(
        {
            "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b&start=10&end=20": ["bob"],
            "http://nutmeg.test/nodes/ada/degree": {
                "total": 1,
                "by_type": {"plays_for": 1},
            },
            "http://nutmeg.test/nodes/ada/degree?edge_type=plays_for": 1,
            "http://nutmeg.test/empty": None,
        }
    )

    assert client.get_neighbors("ada", ["a", "b"], start=10, end=20) == ["bob"]
    assert client.get_degree("ada") == {"total": 1, "by_type": {"plays_for": 1}}
    assert client.get_degree("ada", "plays_for") == 1
    assert client._request("DELETE", "/empty") is None

    assert [call["url"] for call in calls] == [
        "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b&start=10&end=20",
        "http://nutmeg.test/nodes/ada/degree",
        "http://nutmeg.test/nodes/ada/degree?edge_type=plays_for",
        "http://nutmeg.test/empty",
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


def build_set_query(client):
    query = client.query("ada", attributes=True)
    connected = query.follow_edges(
        "connected_to",
        name="connected",
        start=10,
        end=20,
        attributes=True,
        scores=True,
    )
    blocked = query.follow_edges("blocks", name="blocked")
    visible = connected.subtract(blocked, name="visible", degrees=True)
    merged = visible.union(blocked, name="merged")
    visible.intersect(merged, name="visible_again")
    visible.symmetric_difference(blocked, name="changed")
    merged.follow_edges("posted", name="posts")
    return query


def test_query_builder_serializes_server_side_plan():
    query = build_set_query(Nutmeg("http://nutmeg.test"))

    assert query.to_dict() == {
        "wire_version": 1,
        "start_nodes": ["ada"],
        "stage_specs": [
            {"name": "start_stage", "kind": "start", "attributes": True},
            {
                "name": "connected",
                "kind": "follow",
                "sources": ["start_stage"],
                "edge_type": "connected_to",
                "start": 10,
                "end": 20,
                "attributes": True,
                "scores": True,
            },
            {
                "name": "blocked",
                "kind": "follow",
                "sources": ["start_stage"],
                "edge_type": "blocks",
            },
            {
                "name": "visible",
                "kind": "subtract",
                "sources": ["connected", "blocked"],
                "degrees": True,
            },
            {"name": "merged", "kind": "union", "sources": ["visible", "blocked"]},
            {
                "name": "visible_again",
                "kind": "intersect",
                "sources": ["visible", "merged"],
            },
            {
                "name": "changed",
                "kind": "symmetric_difference",
                "sources": ["visible", "blocked"],
            },
            {"name": "posts", "kind": "follow", "sources": ["merged"], "edge_type": "posted"},
        ],
    }


def test_execute_posts_query_once_and_hydrates_result(fake_urlopen):
    responses, calls = fake_urlopen
    client = Nutmeg("http://nutmeg.test", timeout=4)
    responses["http://nutmeg.test/queries/execute"] = {
        "wire_version": 1,
        "stages": {"start_stage": ["ada"], "connected": ["bob"]},
        "nodes": {"bob": {"node_type": "person"}},
        "scores": {"connected": {"bob": 10}},
    }
    query = client.query("ada").follow_edges(
        "connected_to", name="connected", scores=True
    ).query

    result = query.execute()

    assert result.get_nodes("connected") == ["bob"]
    assert query.get_nodes("connected") == ["bob"]
    assert result.get_scores("connected") == {"bob": 10}
    assert calls == [
        {
            "method": "POST",
            "url": "http://nutmeg.test/queries/execute",
            "headers": {"Content-type": "application/json"},
            "timeout": 4,
            "body": query.to_dict(),
        }
    ]


def test_execute_rejects_response_that_does_not_match_query_plan(fake_urlopen):
    responses, _ = fake_urlopen
    client = Nutmeg("http://nutmeg.test")
    responses["http://nutmeg.test/queries/execute"] = {
        "wire_version": 1,
        "stages": {"start_stage": ["ada"], "connected": ["bob"]},
        "nodes": {"bob": {"node_type": "person"}},
        "scores": {"connected": {"bob": 10}},
    }
    query = client.query("ada").follow_edges("connected_to", name="connected").query

    with pytest.raises(ValueError, match="requested score stages"):
        query.execute()


def test_query_roundtrips_through_dict_and_json():
    original = build_set_query(Nutmeg("http://nutmeg.test"))

    from_dict = NutmegQuery.from_dict(Nutmeg("http://nutmeg.test"), original.to_dict())
    from_json = NutmegQuery.from_json(Nutmeg("http://nutmeg.test"), original.to_json())

    assert from_dict.to_dict() == original.to_dict()
    assert from_json.to_dict() == original.to_dict()


def test_result_roundtrips_through_dict_and_json_with_scores():
    result = QueryResult(
        stages={"start_stage": ["ada"], "connected": ["bob"]},
        nodes={"bob": {"node_type": "person", "attributes": {"name": "Bob"}}},
        scores={"connected": {"bob": 10}},
    )

    assert QueryResult.from_dict(result.to_dict()) == result
    assert QueryResult.from_json(result.to_json()) == result


def test_result_loader_rejects_bad_wire_responses():
    with pytest.raises(ValueError, match="response score"):
        QueryResult.from_dict(
            {
                "wire_version": 1,
                "stages": {"connected": ["bob"]},
                "nodes": {"bob": {"node_type": "person"}},
                "scores": {"connected": {"ghost": 10}},
            }
        )


def test_default_stage_names_are_stable_and_human_readable():
    query = Nutmeg("http://nutmeg.test").query("ada")
    connected = query.follow_edges("connected_to")
    blocked = query.follow_edges("blocks")
    connected.union(blocked)

    assert query.topological_stage_names() == [
        "start_stage",
        "stage",
        "stage2",
        "union",
    ]


def test_duplicate_stage_name_is_rejected():
    query = Nutmeg("http://nutmeg.test").query("ada")
    query.follow_edges("connected_to", name="stage")

    with pytest.raises(ValueError, match="already exists"):
        query.follow_edges("blocks", name="stage")


def test_query_builder_rejects_empty_start_nodes():
    with pytest.raises(ValueError, match="at least one start node"):
        Nutmeg("http://nutmeg.test").query([])


def test_query_builder_rejects_stage_handles_from_other_queries():
    query = Nutmeg("http://nutmeg.test").query("ada")
    other_stage = Nutmeg("http://nutmeg.test").query("bob").start

    with pytest.raises(ValueError, match="different query"):
        query.start.union(other_stage)


def test_get_nodes_before_execute_is_rejected():
    query = Nutmeg("http://nutmeg.test").query("ada")

    with pytest.raises(RuntimeError, match="not been executed"):
        query.get_nodes("start_stage")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {
                "wire_version": 1,
                "start_nodes": [],
                "stage_specs": [{"name": "start_stage", "kind": "start"}],
            },
            "at least one start node",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "stage",
                        "kind": "follow",
                        "sources": ["missing"],
                        "edge_type": "connected_to",
                    },
                ],
            },
            "missing stage",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "bad", "kind": "follow", "sources": ["start_stage"]},
                ],
            },
            "requires one source and edge_type",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "bad", "kind": "union", "sources": ["start_stage"]},
                ],
            },
            "requires exactly two sources",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "bad",
                        "kind": "union",
                        "sources": ["start_stage", "start_stage", "start_stage"],
                    },
                ],
            },
            "requires exactly two sources",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "bad",
                        "kind": "follow",
                        "sources": ["start_stage"],
                        "edge_type": "connected_to",
                        "max_edges": 0,
                    },
                ],
            },
            "Unknown stage fields",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {
                        "name": "bad",
                        "kind": "union",
                        "sources": ["start_stage", "start_stage"],
                        "scores": True,
                    },
                ],
            },
            "cannot request scores",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start", "scores": True},
                ],
            },
            "cannot request scores",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "a", "kind": "union", "sources": ["b", "start_stage"]},
                    {"name": "b", "kind": "union", "sources": ["a", "start_stage"]},
                ],
            },
            "cycle",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "start_stage", "kind": "union", "sources": []},
                ],
            },
            "duplicate stage names",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "typo", "kind": "collapse", "sources": ["start_stage"]},
                ],
            },
            "Unknown stage kind",
        ),
        (
            {
                "wire_version": 1,
                "start_nodes": ["ada"],
                "stage_specs": [
                    {"name": "start_stage", "kind": "start"},
                    {"name": "other_start", "kind": "start"},
                ],
            },
            "exactly one start",
        ),
        (
            {
                "wire_version": 2,
                "start_nodes": ["ada"],
                "stage_specs": [{"name": "start_stage", "kind": "start"}],
            },
            "Unsupported query wire_version",
        ),
    ],
)
def test_malformed_queries_are_rejected_when_loading(data, message):
    with pytest.raises(ValueError, match=message):
        NutmegQuery.from_dict(Nutmeg("http://nutmeg.test"), data)


def test_query_json_is_compact_valid_json_and_dedupes_start_nodes():
    query = Nutmeg("http://nutmeg.test").query(["Ada Lovelace", "Ada Lovelace"])
    payload = query.to_json()

    assert json.dumps(json.loads(payload), separators=(",", ":"), sort_keys=True) == payload
    assert json.loads(payload)["start_nodes"] == ["Ada Lovelace"]
    assert "collapse" not in payload
