"""Contract for the NutmegError/StatusCode hierarchy shared by every layer."""

import pytest

from src.status_codes import (
    HTTP_STATUS,
    InvalidQueryError,
    NodeNotFoundError,
    NutmegError,
)


def test_to_dict_includes_reason_only_when_set():
    """Design decision: `reason` is omitted, not null, when an error doesn't have
    one -- keeps a plain NODE_NOT_FOUND body from growing an unused field."""
    without_reason = NodeNotFoundError("node 'ghost' does not exist")
    assert without_reason.to_dict() == {
        "code": "NODE_NOT_FOUND",
        "detail": "node 'ghost' does not exist",
    }

    with_reason = InvalidQueryError("bad plan", reason="MISSING_STAGES")
    assert with_reason.to_dict() == {
        "code": "INVALID_QUERY",
        "detail": "bad plan",
        "reason": "MISSING_STAGES",
    }


def test_str_prefixes_the_code_so_it_survives_plain_text_channels():
    """Some callers (an MCP tool error) only see str(exc), not to_dict() -- the
    code must still be recoverable from there."""
    assert str(NodeNotFoundError("node 'ghost' does not exist")) == (
        "[NODE_NOT_FOUND] node 'ghost' does not exist"
    )
    assert str(InvalidQueryError("bad plan", reason="MISSING_STAGES")) == (
        "[INVALID_QUERY:MISSING_STAGES] bad plan"
    )


def test_every_raisable_code_has_an_http_status():
    """Every StatusCode a NutmegError subclass can carry must map to an HTTP
    status, or api/server.py's exception handler would KeyError on it."""
    for subclass in NutmegError.__subclasses__():
        assert subclass.code in HTTP_STATUS, f"{subclass.__name__} has no HTTP mapping"


def test_nutmeg_error_is_a_value_error():
    """Design decision: NutmegError subclasses ValueError so every existing
    `except ValueError` / `pytest.raises(ValueError)` call site in this codebase
    keeps working unchanged as errors migrate to carry a status code."""
    assert issubclass(NodeNotFoundError, ValueError)
    with pytest.raises(ValueError):
        raise NodeNotFoundError("node 'ghost' does not exist")
