"""Status/error codes clients use to understand what happened to a request.

The top-level list is kept deliberately short -- clients switch on `code`.
Where more detail is useful without growing that list, an error also carries
a `reason`: a code-specific string (e.g. INVALID_QUERY's DUPLICATE_STAGE_NAME).
See docs/status_codes.md for the full, documented list.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class StatusCode(str, Enum):
    OK = "OK"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    NODE_NOT_FOUND = "NODE_NOT_FOUND"
    MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"
    INVALID_QUERY = "INVALID_QUERY"
    UNAUTHORIZED = "UNAUTHORIZED"
    BULK_PARTIAL_FAILURE = "BULK_PARTIAL_FAILURE"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    DATA_ERROR = "DATA_ERROR"
    # Earns a top-level code despite the "keep this list short" rule above: it is
    # the only code that blames the *peer* rather than the caller's request, so
    # folding it into INVALID_QUERY would tell a client its own input was bad when
    # the truth is the other end sent something unusable.
    INVALID_RESPONSE = "INVALID_RESPONSE"


# HTTP status each code maps to at the FastAPI boundary. BULK_PARTIAL_FAILURE
# has no entry -- bulk-load responses carry it as a 200 body field, not a
# raised error, since the request itself still succeeded. RESOURCE_LIMIT_EXCEEDED's
# entry here is a fallback (see _RESOURCE_LIMIT_HTTP_STATUS below for its
# reason-specific mappings) -- 429 stays free for a future RATE_LIMITED reason,
# the one HTTP status 429 ("Too Many Requests") actually describes.
HTTP_STATUS: dict[StatusCode, int] = {
    StatusCode.SERVICE_UNAVAILABLE: 503,
    StatusCode.NODE_NOT_FOUND: 404,
    StatusCode.MAX_DEPTH_EXCEEDED: 400,
    StatusCode.INVALID_QUERY: 400,
    StatusCode.UNAUTHORIZED: 401,
    StatusCode.RESOURCE_LIMIT_EXCEEDED: 429,
    StatusCode.DATA_ERROR: 409,
    # 500: the only way this reaches the HTTP boundary is QueryExecutor's check of
    # the response it just built (see query_engine._execute), i.e. the server
    # produced something invalid. That is a server fault, not a bad request.
    StatusCode.INVALID_RESPONSE: 500,
}

# RESOURCE_LIMIT_EXCEEDED reasons that have a more specific HTTP status than the
# 429 fallback above: 504 for a server-side execution timeout, 413 for a result
# known to be too large to return.
_RESOURCE_LIMIT_HTTP_STATUS: dict[str, int] = {
    "TIMEOUT": 504,
    "RESULT_TOO_LARGE": 413,
}


class NutmegError(ValueError):
    """Base for errors that carry a `StatusCode` clients can switch on.

    Subclasses ValueError (rather than Exception) so every existing
    `except ValueError` / `pytest.raises(ValueError)` call site keeps working
    unchanged -- these are a more specific kind of the same "bad request /
    bad state" error, not a new category.
    """

    code: StatusCode

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason

    @property
    def message(self) -> str:
        return self.args[0]

    def __str__(self) -> str:
        # A `[CODE]` (or `[CODE:REASON]`) prefix so the code survives even where
        # only the exception's text reaches a client -- e.g. an MCP tool error,
        # which reports str(exc) with no separate structured field.
        tag = self.code.value if self.reason is None else f"{self.code.value}:{self.reason}"
        return f"[{tag}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code.value, "detail": self.message}
        if self.reason is not None:
            data["reason"] = self.reason
        return data

    @property
    def http_status(self) -> int:
        if self.code == StatusCode.RESOURCE_LIMIT_EXCEEDED and self.reason in _RESOURCE_LIMIT_HTTP_STATUS:
            return _RESOURCE_LIMIT_HTTP_STATUS[self.reason]
        return HTTP_STATUS[self.code]


class ServiceUnavailableError(NutmegError):
    """The graph store (Redis) could not be reached or timed out."""

    code = StatusCode.SERVICE_UNAVAILABLE


class NodeNotFoundError(NutmegError):
    """A well-formed node id does not exist in the graph."""

    code = StatusCode.NODE_NOT_FOUND


class MaxDepthExceededError(NutmegError):
    """A query plan chains more stages than the server allows to traverse."""

    code = StatusCode.MAX_DEPTH_EXCEEDED


class InvalidQueryError(NutmegError):
    """A request document is malformed -- see `reason` for which rule failed."""

    code = StatusCode.INVALID_QUERY


class UnauthorizedError(NutmegError):
    """Reserved for future authentication support; nothing raises this yet."""

    code = StatusCode.UNAUTHORIZED


class ResourceLimitExceededError(NutmegError):
    """A request exceeded a runtime resource limit.

    reason distinguishes TIMEOUT (took too long) from RESULT_TOO_LARGE
    (would return too much data).
    """

    code = StatusCode.RESOURCE_LIMIT_EXCEEDED


class InvalidResponseError(NutmegError):
    """A protocol peer sent a response this end cannot accept.

    reason distinguishes INVALID_RESPONSE_JSON (the body didn't parse at all)
    from INVALID_RESPONSE_DOCUMENT (it parsed but breaks the response contract
    in src/query_response.py). Unlike every other code here, this one is about
    the sender's output rather than the caller's input -- the client raises it
    when the server misbehaves, and the server raises it against itself when the
    response it assembled fails its own outgoing check.
    """

    code = StatusCode.INVALID_RESPONSE


class DataError(NutmegError):
    """The graph rejected an otherwise well-formed write because of the data's
    own state -- e.g. reason=NODE_TYPE_CONFLICT for re-adding a node under a
    different (immutable) type. Distinct from INVALID_QUERY, which is about a
    malformed request rather than a conflict with existing data."""

    code = StatusCode.DATA_ERROR
