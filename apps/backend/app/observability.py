"""OpenTelemetry wiring, per ARCHITECTURE.md § Observability: log assistant
requests, retrieved context identifiers, tool calls, tool result metadata,
latency, and errors — never secrets. No large observability stack is
required for V1 (ADR-017): with no `OTEL_EXPORTER_OTLP_ENDPOINT` configured,
spans are exported through the stdlib `logging` module instead of over the
network, so instrumentation is always on with zero required infrastructure.
"""
from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

logger = logging.getLogger("tars.tracing")


class LoggingSpanExporter(SpanExporter):
    """No-network default exporter — writes each finished span's key
    fields (name, duration, attributes, status) through the standard
    logger. Attributes are set explicitly at each instrumented call site
    (never raw request/response bodies), so secrets never reach here in
    the first place — this exporter does not additionally redact anything.
    """

    def export(self, spans) -> SpanExportResult:  # type: ignore[override]
        for span in spans:
            self._log_span(span)
        return SpanExportResult.SUCCESS

    def _log_span(self, span: ReadableSpan) -> None:
        start_time, end_time = span.start_time, span.end_time
        duration_ms = (end_time - start_time) / 1_000_000 if start_time and end_time else None
        attrs = dict(span.attributes or {})
        status = span.status.status_code.name if span.status else "UNSET"
        logger.info(
            "span=%s duration_ms=%s status=%s attrs=%s",
            span.name,
            f"{duration_ms:.1f}" if duration_ms is not None else "n/a",
            status,
            attrs,
        )

    def shutdown(self) -> None:  # pragma: no cover - nothing to release
        pass


def configure_tracing(service_name: str, otlp_endpoint: str | None) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
        logger.info("tracing: exporting to OTLP endpoint %s", otlp_endpoint)
    else:
        provider.add_span_processor(SimpleSpanProcessor(LoggingSpanExporter()))
        logger.info("tracing: no OTEL_EXPORTER_OTLP_ENDPOINT set, logging spans locally")

    trace.set_tracer_provider(provider)
    return provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("tars.backend")
