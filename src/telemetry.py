"""OpenTelemetry wiring, shared by the API and MCP servers.

Telemetry is opt-in by the presence of OTEL_EXPORTER_OTLP_ENDPOINT -- there is no
nutmeg-specific flag, because the standard OTEL_* environment variables already
say everything worth saying and every other knob (protocol, headers, timeouts,
sampling) is read by the SDK itself. With the var unset this module does nothing,
so `pytest` and a bare `python main.py` are untouched.

Two scopes are deliberately kept apart. The SDK providers are process-global and
installed once; instrumenting an app is per-app and happens on every call. Folding
the two together would mean that a process which set telemetry up for the MCP
server first (by importing it first, say) would then silently skip instrumenting
the API app.

The opentelemetry imports live inside the functions rather than at module scope on
purpose: they are an optional dependency group (`pip install '.[otel]'`), and an
install without them must still be able to import the servers.

FastMCP needs no instrumentation package: it emits its own spans through
opentelemetry-api, which are no-ops until a real TracerProvider is registered.
Registering one here is the whole hook.
"""

import os
import warnings

ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"

# The service name the global providers were built with, or None if unconfigured.
_provider_service_name: str | None = None


def setup_telemetry(service_name: str, app=None) -> bool:
    """Export OTLP traces and metrics. No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set.

    `app`, when given, is the FastAPI app to instrument; the MCP server has no
    ASGI app to hand over and relies on FastMCP's own spans instead. Returns
    whether telemetry is on, which is what the tests assert on.
    """
    if not os.environ.get(ENDPOINT_VAR):
        return False

    _configure_providers(service_name)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Unconditional: the providers may already have been installed by another
        # server in this process, but this app still needs its own middleware.
        # instrument_app is itself a no-op on an app it has already instrumented.
        FastAPIInstrumentor.instrument_app(app)

    return True


def _configure_providers(service_name: str) -> None:
    """Install the global SDK providers. Runs once per process.

    A resource -- service.name included -- describes the process, so a process
    running both servers can only report one name. That is a deployment mistake
    rather than something to paper over, hence the warning.
    """
    global _provider_service_name

    resolved = os.environ.get("OTEL_SERVICE_NAME", service_name)
    if _provider_service_name is not None:
        if _provider_service_name != resolved:
            warnings.warn(
                f"OpenTelemetry is already configured as {_provider_service_name!r}, "
                f"so {resolved!r}'s telemetry will be reported under that name. Run "
                "the API and MCP servers in separate processes to tell them apart.",
                stacklevel=3,
            )
        return

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": resolved})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )

    # Class-level patching, so the module-scope clients built before this call are
    # covered too.
    RedisInstrumentor().instrument()

    _provider_service_name = resolved
