"""Adapter turning a graph-layer Python exception into a status-coded `NutmegError`.

graph.py, graph_writes.py, and bulk_redis.py deliberately raise plain Python
exceptions (see graph_writes.py's module docstring) so they stay usable outside
any protocol context. Every public surface -- the HTTP API and the MCP server --
owes callers the same `code`/`reason` contract documented in
docs/status_codes.md, so that translation lives here once rather than being
copied into each surface and drifting.
"""

from __future__ import annotations

import functools

import redis.exceptions

from src.graph import NodeTypeConflictError
from src.graph_writes import InvalidIdentifierError
from src.status_codes import DataError, InvalidQueryError, NutmegError, ServiceUnavailableError


def as_nutmeg_error(exc: Exception) -> NutmegError:
    """The status-coded equivalent of a graph-layer exception.

    Anything not recognised more specifically is still a rejection of the given
    data rather than a server fault, so it becomes a reason-less DataError
    instead of an unhandled 500 / uncoded MCP error.
    """
    if isinstance(exc, NutmegError):
        return exc
    if isinstance(exc, InvalidIdentifierError):
        return InvalidQueryError(str(exc), reason="INVALID_IDENTIFIER")
    if isinstance(exc, NodeTypeConflictError):
        return DataError(str(exc), reason="NODE_TYPE_CONFLICT")
    if isinstance(exc, redis.exceptions.RedisError):
        # The call may never have reached Redis, so it's SERVICE_UNAVAILABLE
        # ("safe to retry") rather than a generic fault.
        return ServiceUnavailableError(f"redis error: {exc}")
    return DataError(str(exc))


def status_coded(tool):
    """Decorator re-raising a tool's graph-layer exception as its status-coded
    equivalent. For surfaces that propagate exceptions rather than build a
    response from them (the MCP server), where `NutmegError.__str__`'s `[CODE]`
    prefix is all the caller gets to switch on."""

    @functools.wraps(tool)
    async def with_status_code(*args, **kwargs):
        try:
            return await tool(*args, **kwargs)
        except (ValueError, redis.exceptions.RedisError) as exc:
            raise as_nutmeg_error(exc) from exc

    return with_status_code
