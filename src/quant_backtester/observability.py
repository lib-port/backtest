from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "qbt_http_requests_total",
    "HTTP requests handled by the control plane.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "qbt_http_request_duration_seconds",
    "Control-plane request latency.",
    ("method", "route"),
)
JOBS = Counter(
    "qbt_jobs_total",
    "Worker job attempts by outcome.",
    ("outcome",),
)
JOB_DURATION = Histogram(
    "qbt_job_duration_seconds",
    "Backtest worker attempt duration.",
    ("outcome",),
)
INVARIANT_FAILURES = Counter(
    "qbt_invariant_failures_total",
    "Accounting or execution invariant violations.",
    ("invariant",),
)

_telemetry_configured = False
_tracer_provider: TracerProvider | None = None


def configure_telemetry(*, service_name: str) -> None:
    global _telemetry_configured, _tracer_provider
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint and not _telemetry_configured:
        sample_ratio = float(os.getenv("QBT_TRACE_SAMPLE_RATIO", "0.1"))
        if not 0.0 <= sample_ratio <= 1.0:
            raise ValueError("QBT_TRACE_SAMPLE_RATIO must be in [0, 1]")
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.version": os.getenv("QBT_SOURCE_REVISION", "development"),
                    "deployment.environment.name": os.getenv(
                        "QBT_ENVIRONMENT",
                        "development",
                    ),
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        _telemetry_configured = True


def shutdown_telemetry() -> None:
    global _telemetry_configured, _tracer_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    _tracer_provider = None
    _telemetry_configured = False


def instrument_app(app: FastAPI, *, service_name: str) -> None:
    configure_telemetry(service_name=service_name)
    FastAPIInstrumentor.instrument_app(app)
