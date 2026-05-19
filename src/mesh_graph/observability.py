from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from mesh_graph.config import ObservabilityConfig

logger = logging.getLogger(__name__)
_configured = False
_enabled = False
_duration_hist = None
_error_counter = None


def _sanitize_attr_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {k: _sanitize_attr_value(v) for k, v in attributes.items()}


def configure_observability(cfg: ObservabilityConfig) -> None:
    global _configured, _enabled, _duration_hist, _error_counter
    _enabled = cfg.enabled
    if _configured or not cfg.enabled:
        return

    resource = Resource.create(
        {
            SERVICE_NAME: cfg.service_name,
            "deployment.environment": cfg.environment,
        }
    )

    sampler = ParentBased(TraceIdRatioBased(cfg.sample_ratio))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    if cfg.exporter == "console":
        span_exporter = ConsoleSpanExporter()
    elif cfg.exporter == "otlp":
        span_exporter = OTLPSpanExporter(endpoint=cfg.otlp_endpoint, insecure=True)
    else:
        raise ValueError(f"Unsupported observability exporter '{cfg.exporter}'")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    meter = None
    if cfg.exporter == "console":
        metric_exporter = ConsoleMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter("mesh_graph.observability")
    elif cfg.exporter == "otlp":
        logger.info(
            "Skipping OTLP metric export for Jaeger endpoint %s; traces remain enabled",
            cfg.otlp_endpoint,
        )

    if meter is not None:
        _duration_hist = meter.create_histogram(
            "mesh_graph.stage.duration.ms",
            unit="ms",
            description="Duration of instrumented stages",
        )
        _error_counter = meter.create_counter(
            "mesh_graph.stage.errors",
            unit="1",
            description="Count of instrumented stage failures",
        )

    _configured = True
    logger.info(
        "OpenTelemetry enabled: exporter=%s endpoint=%s sample_ratio=%s",
        cfg.exporter,
        cfg.otlp_endpoint,
        cfg.sample_ratio,
    )


def instrument_fastapi(app) -> None:
    if _enabled:
        FastAPIInstrumentor.instrument_app(app)


@contextmanager
def traced_span(
    name: str,
    *,
    warn_ms: float | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    attrs = _sanitize_attributes(attributes)
    start = time.perf_counter()
    tracer = trace.get_tracer("mesh_graph")
    with tracer.start_as_current_span(name) as span:
        for key, value in attrs.items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            if _error_counter is not None:
                _error_counter.add(1, {"stage": name})
            span.record_exception(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("stage.duration_ms", elapsed_ms)
            if warn_ms is not None and elapsed_ms >= warn_ms:
                span.add_event(
                    "slow_stage",
                    {
                        "warn_ms": warn_ms,
                        "elapsed_ms": elapsed_ms,
                    },
                )
            if _duration_hist is not None:
                _duration_hist.record(elapsed_ms, {"stage": name})
