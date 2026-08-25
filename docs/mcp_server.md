

MCP clients can send the query plan directly as JSON; they do not need access to
the Python client or repository files. The `run_query` tool accepts wire version
1 with one non-empty `start_nodes` list and one `start` stage. A `follow` stage
has one source and an `edge_type`; `union`, `intersect`, `subtract`, and
`symmetric_difference` stages each have two sources. Sources are stage names,
and dependencies must be acyclic.

```json
{
  "wire_version": 1,
  "start_nodes": ["ada"],
  "stage_specs": [
    {"name": "start", "kind": "start"},
    {"name": "teams", "kind": "follow", "sources": ["start"],
     "edge_type": "plays_for", "attributes": true, "scores": true}
  ]
}
```

`follow` stages may also use numeric inclusive `start` and `end` score bounds.
Any stage may request `degrees` or `attributes`; only `follow` stages may request
`scores`. Call `get_meta_graph` first to discover the graph's node and edge types.

## Response Shape

The response keeps stage outputs compact:

```json
{
  "wire_version": 1,
  "stages": {
    "start_stage": ["ada"],
    "connected": ["bob", "cara"],
    "blocked": ["erin"],
    "visible": ["bob", "cara"]
  },
  "nodes": {
    "bob": {
      "node_type": "person",
      "attributes": {"name": "Bob"},
      "degree": {"total": 1, "by_type": {"posted": 1}}
    }
  },
  "scores": {
    "connected": {"bob": 10.0, "cara": 20.0}
  }
}
```

`stages` are always lists of node ids. `nodes` contains only metadata requested
by stages with `attributes=True` or `degrees=True`. `scores` is present only for
traversal stages with `scores=True`; set-operation stages never emit scores.

Query plans and results round-trip cleanly:

```python
from src.client import QueryResult

saved_query = query.to_json()
query = nutmeg.query_from_json(saved_query)

saved_result = result.to_json()
result = QueryResult.from_json(saved_result)
```