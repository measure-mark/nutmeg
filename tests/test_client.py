"""Python client query construction and async HTTP behavior."""

import json

import httpx
import pytest

from src.client import NutmegClient, NutmegHTTPError, NutmegQuery, QueryResult


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    responses = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.timeout = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, *, params=None, json=None):
            full_url = str(httpx.URL(url, params=params))
            calls.append({
                "method": method,
                "url": full_url,
                "headers": {"Content-type": "application/json"} if json is not None else {},
                "timeout": self.timeout,
                "body": json,
            })
            response = responses[full_url]
            if isinstance(response, tuple):
                status, body = response
                response = httpx.Response(status, text=body) if isinstance(body, str) else httpx.Response(status, json=body)
                response.request = httpx.Request(method, full_url)
                return response
            if response is None:
                return httpx.Response(204, request=httpx.Request(method, full_url))
            return httpx.Response(200, json=response, request=httpx.Request(method, full_url))

    monkeypatch.setattr("src.client.httpx.AsyncClient", FakeAsyncClient)
    return responses, calls


def make_http_error(body, status_code=400):
    return status_code, body


async def test_http_client_builds_direct_requests_and_handles_empty_responses(fake_http):
    responses, calls = fake_http
    client = NutmegClient("http://nutmeg.test", timeout=3)
    responses.update(
        {
            "http://nutmeg.test/nodes/ada": {"node_type": "person"},
            "http://nutmeg.test/nodes/ada/degree": {"total": 1, "by_type": {"friend": 1}},
            "http://nutmeg.test/nodes/ada/degree?edge_type=friend": 1,
            "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b&start=10&end=20": [
                "bob"
            ],
            "http://nutmeg.test/empty": None,
        }
    )

    assert await client.get_node("ada") == {"node_type": "person"}
    assert await client.get_degree("ada") == {"total": 1, "by_type": {"friend": 1}}
    assert await client.get_degree("ada", "friend") == 1
    assert await client.get_neighbors("ada", ["a", "b"], start=10, end=20) == ["bob"]
    assert await client._request("DELETE", "/empty") is None
    assert [call["url"] for call in calls] == [
        "http://nutmeg.test/nodes/ada",
        "http://nutmeg.test/nodes/ada/degree",
        "http://nutmeg.test/nodes/ada/degree?edge_type=friend",
        "http://nutmeg.test/nodes/ada/neighbors?edge_types=a&edge_types=b&start=10&end=20",
        "http://nutmeg.test/empty",
    ]


async def test_http_client_extracts_json_and_plain_error_details(fake_http):
    responses, _ = fake_http
    client = NutmegClient("http://nutmeg.test")

    responses["http://nutmeg.test/nodes/json-error"] = make_http_error(
        {"detail": "node missing"}
    )
    with pytest.raises(NutmegHTTPError) as exc:
        await client.get_node("json-error")
    assert exc.value.detail == "node missing"

    responses["http://nutmeg.test/nodes/plain-error"] = make_http_error("plain bad")
    with pytest.raises(NutmegHTTPError) as exc:
        await client.get_node("plain-error")
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


async def test_query_builder_serializes_server_side_plan():
    query = build_set_query(NutmegClient("http://nutmeg.test"))

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


async def test_execute_posts_query_once_and_hydrates_result(fake_http):
    responses, calls = fake_http
    client = NutmegClient("http://nutmeg.test", timeout=4)
    responses["http://nutmeg.test/queries/execute"] = {
        "wire_version": 1,
        "stages": {"start_stage": ["ada"], "connected": ["bob"]},
        "nodes": {"bob": {"node_type": "person"}},
        "scores": {"connected": {"bob": 10}},
    }
    query = client.query("ada").follow_edges(
        "connected_to", name="connected", scores=True
    ).query

    result = await query.execute()

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


async def test_execute_rejects_response_that_does_not_match_requested_scores(fake_http):
    responses, _ = fake_http
    client = NutmegClient("http://nutmeg.test")
    responses["http://nutmeg.test/queries/execute"] = {
        "wire_version": 1,
        "stages": {"start_stage": ["ada"], "connected": ["bob"]},
        "nodes": {"bob": {"node_type": "person"}},
    }
    query = client.query("ada").follow_edges(
        "connected_to",
        name="connected",
        scores=True,
    ).query

    with pytest.raises(ValueError, match="requested score stages"):
        await query.execute()


async def test_query_roundtrips_through_dict_and_json():
    original = build_set_query(NutmegClient("http://nutmeg.test"))

    from_dict = NutmegQuery.from_dict(NutmegClient("http://nutmeg.test"), original.to_dict())
    from_json = NutmegQuery.from_json(NutmegClient("http://nutmeg.test"), original.to_json())

    assert from_dict.to_dict() == original.to_dict()
    assert from_json.to_dict() == original.to_dict()


def test_client_query_wrappers_use_shared_wire_validation():
    client = NutmegClient("http://nutmeg.test")
    query = build_set_query(client)

    assert client.query_from_dict(query.to_dict()).to_dict() == query.to_dict()
    assert client.query_from_json(query.to_json()).to_dict() == query.to_dict()


async def test_result_roundtrips_through_dict_and_json_with_scores():
    result = QueryResult(
        stages={"start_stage": ["ada"], "connected": ["bob"]},
        nodes={"bob": {"node_type": "person", "attributes": {"name": "Bob"}}},
        scores={"connected": {"bob": 10}},
    )

    assert QueryResult.from_dict(result.to_dict()) == result
    assert QueryResult.from_json(result.to_json()) == result


async def test_result_loader_rejects_bad_wire_responses():
    with pytest.raises(ValueError, match="response score"):
        QueryResult.from_dict(
            {
                "wire_version": 1,
                "stages": {"connected": ["bob"]},
                "nodes": {"bob": {"node_type": "person"}},
                "scores": {"connected": {"ghost": 10}},
            }
        )


async def test_default_stage_names_are_stable_and_human_readable():
    query = NutmegClient("http://nutmeg.test").query("ada")
    connected = query.follow_edges("connected_to")
    blocked = query.follow_edges("blocks")
    connected.union(blocked)

    assert list(query._stages) == [
        "start_stage",
        "stage",
        "stage2",
        "union",
    ]


async def test_duplicate_stage_name_is_rejected():
    query = NutmegClient("http://nutmeg.test").query("ada")
    query.follow_edges("connected_to", name="stage")

    with pytest.raises(ValueError, match="already exists"):
        query.follow_edges("blocks", name="stage")


async def test_query_builder_rejects_empty_start_nodes():
    with pytest.raises(ValueError, match="at least one start node"):
        NutmegClient("http://nutmeg.test").query([])


async def test_query_builder_rejects_stage_handles_from_other_queries():
    query = NutmegClient("http://nutmeg.test").query("ada")
    other_stage = NutmegClient("http://nutmeg.test").query("bob").start

    with pytest.raises(ValueError, match="different query"):
        query.start.union(other_stage)


async def test_get_nodes_before_execute_is_rejected():
    query = NutmegClient("http://nutmeg.test").query("ada")

    with pytest.raises(RuntimeError, match="not been executed"):
        query.get_nodes("start_stage")

    with pytest.raises(RuntimeError, match="not been executed"):
        query.get_scores("start_stage")


async def test_query_json_is_compact_valid_json_and_dedupes_start_nodes():
    query = NutmegClient("http://nutmeg.test").query(["Ada Lovelace", "Ada Lovelace"])
    payload = query.to_json()

    assert json.dumps(json.loads(payload), separators=(",", ":"), sort_keys=True) == payload
    assert json.loads(payload)["start_nodes"] == ["Ada Lovelace"]
