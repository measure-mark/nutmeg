"""Contracts around OpenTelemetry wiring (src/telemetry.py).

Only a few things need enforcing here: that telemetry stays off by default, that
instrumentation does not disturb the status-code contract, and that setting one
server up does not rob the other of instrumentation. The rest -- what a span is
named, which attributes it carries -- belongs to the instrumentation libraries,
not to us.
"""

import warnings

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import src.telemetry as telemetry

# The flag FastAPIInstrumentor sets on an app it has instrumented -- and reads to stay
# idempotent. It patches build_middleware_stack rather than appending to
# user_middleware, so this is the attribute that says whether an app is instrumented.
INSTRUMENTED = "_is_instrumented_by_opentelemetry"


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    """Each test starts from "telemetry has not been set up yet"."""
    monkeypatch.setattr(telemetry, "_provider_service_name", None)


@pytest.fixture
def stub_providers(monkeypatch):
    """Record calls to _configure_providers instead of installing real SDK providers,
    which would leak a live OTLP exporter into the rest of the session."""
    calls = []

    def fake(service_name, *, fallback=False):
        calls.append(service_name)
        monkeypatch.setattr(telemetry, "_provider_service_name", service_name)

    monkeypatch.setattr(telemetry, "_configure_providers", fake)
    return calls


def test_no_op_without_endpoint(monkeypatch):
    """Telemetry is off unless OTEL_EXPORTER_OTLP_ENDPOINT is set: this is what keeps
    the test suite and a bare `python main.py` free of exporters -- and free of the
    optional opentelemetry dependencies, which would fail to import if absent."""
    monkeypatch.delenv(telemetry.ENDPOINT_VAR, raising=False)
    app = FastAPI()

    assert telemetry.setup_telemetry("nutmeg-test", app) is False
    assert getattr(app, INSTRUMENTED, False) is False


def test_second_call_still_instruments_its_app(monkeypatch, stub_providers):
    """Regression guard: a process that sets the MCP server up first (by importing it
    first) must still instrument the API app on the second call. Global providers are
    installed once; instrumenting an app is per-app."""
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    monkeypatch.setenv(telemetry.ENDPOINT_VAR, "http://127.0.0.1:4318")
    app = FastAPI()

    telemetry.setup_telemetry("nutmeg-mcp")
    assert telemetry.setup_telemetry("nutmeg-api", app) is True

    assert getattr(app, INSTRUMENTED, False) is True, "second call skipped its app"
    # Both calls delegate; the once-per-process guard lives inside
    # _configure_providers itself, covered by the warning test below.
    assert stub_providers == ["nutmeg-mcp", "nutmeg-api"]


def test_warns_when_a_second_service_name_is_requested(monkeypatch):
    """One process reports one service.name, so a second, different name is a
    deployment mistake worth saying out loud rather than dropping silently."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr(telemetry, "_provider_service_name", "nutmeg-mcp")

    with pytest.warns(UserWarning, match="nutmeg-mcp"):
        telemetry._configure_providers("nutmeg-api")


def test_a_fallback_caller_loses_the_name_without_warning(monkeypatch):
    """The client offers 'nutmeg-client' in case the process has no name of its own.
    Losing to a server that got there first is the outcome it asked for, not the
    misconfiguration the warning is about -- so it must stay quiet."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr(telemetry, "_provider_service_name", "nutmeg-api")

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning fails the test
        telemetry._configure_providers("nutmeg-client", fallback=True)

    assert telemetry._provider_service_name == "nutmeg-api"


def test_value_error_handler_survives_instrumentation():
    """Regression guard: FastAPI instrumentation adds ASGI middleware that sees every
    exception, and src/api/server.py's whole status-code contract rests on a handler
    registered for bare ValueError. The middleware must re-raise, not swallow.

    Instruments the app directly rather than through setup_telemetry, which would
    install global providers and a live OTLP exporter for the rest of the session."""
    fastapi_instrumentation = pytest.importorskip("opentelemetry.instrumentation.fastapi")
    app = FastAPI()

    @app.exception_handler(ValueError)
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"code": "INVALID_QUERY"})

    @app.get("/boom")
    async def boom():
        raise ValueError("bad node id")

    fastapi_instrumentation.FastAPIInstrumentor.instrument_app(app)

    response = TestClient(app).get("/boom")
    assert response.status_code == 422
    assert response.json() == {"code": "INVALID_QUERY"}
