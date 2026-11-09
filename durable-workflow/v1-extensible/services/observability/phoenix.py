import logging
import os
from typing import Optional
from core.interfaces.telemetry import BaseTracer
from services.observability.tracer import ModularTracer

logger = logging.getLogger("observability.phoenix")


def setup_phoenix_tracing(
    service_name: str = "agent-worker",
    phoenix_endpoint: Optional[str] = None,
) -> BaseTracer:
    """
    Initializes Arize Phoenix tracing if dependencies and configuration are present.
    Supports either direct Arize Phoenix OTel collector or local instance.

    Environment variables:
        PHOENIX_COLLECTOR_ENDPOINT: e.g. "http://phoenix.monitoring.svc.cluster.local:6006/v1/traces"
        ENABLE_PHOENIX: "true" | "false"
    """
    endpoint = phoenix_endpoint or os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
    enabled = os.getenv("ENABLE_PHOENIX", "false").lower() in ("true", "1", "yes")

    if not enabled and not endpoint:
        logger.info("Phoenix tracing is disabled or not configured. Using ModularTracer fallback.")
        return ModularTracer(service_name=service_name)

    try:
        # Check if opentelemetry or phoenix is available
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        target_endpoint = endpoint or "http://localhost:6006/v1/traces"
        logger.info(f"Configuring OpenTelemetry / Phoenix exporter towards {target_endpoint}")

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=target_endpoint))
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

        otel_tracer = trace.get_tracer(service_name)
        logger.info("Phoenix OpenTelemetry tracing successfully initialized.")
        return ModularTracer(otel_tracer=otel_tracer, service_name=service_name)

    except ImportError:
        logger.warning(
            "OpenTelemetry / Phoenix libraries not installed. Run 'pip install openinference-instrumentation opentelemetry-exporter-otlp arize-phoenix' to activate full Phoenix UI tracing. Falling back to structured logging."
        )
        return ModularTracer(service_name=service_name)
    except Exception as exc:
        logger.error(f"Failed to configure Phoenix tracing: {exc}. Using fallback.")
        return ModularTracer(service_name=service_name)
