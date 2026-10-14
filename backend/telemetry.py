"""OpenTelemetry tracing setup.

Off by default — call sites use ``tracer.start_as_current_span(...)``
unconditionally and pay only the cost of a no-op span if the SDK isn't
configured. Setting ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g.
``http://otel-collector:4318``) wires the OTLP/HTTP exporter; setting
``OTEL_CONSOLE_EXPORTER=true`` adds a stdout exporter for local debugging.

What gets traced:
  * HTTP requests — auto via FastAPI instrumentation. Health probes
    excluded so the spans aren't flooded with health-check noise.
  * Outbound httpx calls (OpenRouter) — auto via httpx instrumentation.
  * The chat pipeline phases — manual spans started by main.chat()
    and rag.retrieve(): rewrite, embed_query, retrieve.{dense,bm25},
    rerank, llm.first_token, llm.complete.

What is intentionally NOT in span attributes:
  * The user's query text.
  * Document / chunk content.
  * User emails or other PII.
Only counts, scores, IDs, and model names. SECURITY.md A09 covers logs;
the same rule applies to span attributes since both leave the host.

We don't instrument psycopg3 — there is no first-party instrumentation
for it that's stable enough to pin. Postgres timing shows up
indirectly inside the manual retrieve.{dense,bm25} spans.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.trace import Tracer

log = logging.getLogger(__name__)

_initialised = False
SERVICE_NAME = "wissensplattform-backend"


def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def init() -> None:
    """Configure the global tracer provider. Idempotent.

    Call from FastAPI's lifespan startup, AFTER settings have been read.
    Safe to call when no exporter is configured: the SDK still installs a
    TracerProvider, spans are created, no spans are exported.

    Auto-instrumentation for FastAPI is applied by ``instrument_app()``
    (separate call so the app object is in scope at the use site).
    """
    global _initialised
    if _initialised:
        return

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    use_console = _truthy(os.getenv("OTEL_CONSOLE_EXPORTER"))

    # Resource attributes follow the OTel semconv. service.version is left
    # to the deployer's OTEL_RESOURCE_ATTRIBUTES env if they care.
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        # Lazy import keeps the OTLP HTTP dep out of the cold path when
        # tracing is disabled — saves ~50 ms on cold start in tests.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces"))
        )
        log.info("telemetry: OTLP exporter -> %s", otlp_endpoint)
    if use_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        log.info("telemetry: console exporter enabled")

    if not otlp_endpoint and not use_console:
        # No exporter — install the provider anyway so callers get real
        # tracer objects (not the no-op default). Spans are dropped at
        # the end of the processor chain since there is no processor.
        log.info("telemetry: no exporter configured (spans inert)")

    trace.set_tracer_provider(provider)

    # httpx auto-instrumentation patches the client class. Idempotent
    # under the instrumentor — multiple init() calls won't double-wrap.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # pragma: no cover — instrumentation is best-effort
        log.exception("telemetry: httpx instrumentation failed")

    _initialised = True


def instrument_app(app) -> None:
    """Apply FastAPI auto-instrumentation. Call once on the configured
    app object. ``/health`` is excluded so the trace stream isn't filled
    with health-check spans (kubelet/docker probe it every 10 s)."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health")
    except Exception:  # pragma: no cover
        log.exception("telemetry: FastAPI instrumentation failed")


def get_tracer(name: str = "wissensplattform") -> Tracer:
    """Return a tracer. Safe to call before init() — the global
    no-op provider hands out real-shaped (but inert) span objects."""
    return trace.get_tracer(name)
