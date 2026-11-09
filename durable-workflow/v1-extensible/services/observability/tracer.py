import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional
from core.interfaces.telemetry import BaseSpan, BaseTracer

logger = logging.getLogger("observability.tracer")


class OTelSpanWrapper(BaseSpan):
    """
    Wraps an OpenTelemetry span (used by Phoenix or standard OTel exporters).
    """

    def __init__(self, otel_span: Any):
        self._span = otel_span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span and hasattr(self._span, "set_attribute"):
            # Ensure scalar types or string representations
            if isinstance(value, (int, float, str, bool)):
                self._span.set_attribute(key, value)
            else:
                self._span.set_attribute(key, str(value))

    def record_exception(self, exception: Exception) -> None:
        if self._span and hasattr(self._span, "record_exception"):
            self._span.record_exception(exception)


class LoggingSpan(BaseSpan):
    """
    Lightweight structured logging span when OpenTelemetry/Phoenix is not active.
    """

    def __init__(self, name: str, span_type: str, initial_attrs: Dict[str, Any]):
        self.name = name
        self.span_type = span_type
        self.attributes = dict(initial_attrs)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exception: Exception) -> None:
        logger.error(f"[Span:{self.name}] Exception recorded: {exception}", exc_info=True)


class ModularTracer(BaseTracer):
    """
    Modular tracer supporting:
    - OpenTelemetry / Arize Phoenix tracing (if installed/configured)
    - Structured Logging fallback
    """

    def __init__(self, otel_tracer: Optional[Any] = None, service_name: str = "agent-platform"):
        self.otel_tracer = otel_tracer
        self.service_name = service_name

    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        span_type: str = "agent_step",
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[BaseSpan]:
        attrs = attributes or {}
        attrs["service.name"] = self.service_name
        attrs["agent.span_type"] = span_type

        if self.otel_tracer:
            try:
                with self.otel_tracer.start_as_current_span(name) as otel_span:
                    wrapper = OTelSpanWrapper(otel_span)
                    for k, v in attrs.items():
                        wrapper.set_attribute(k, v)
                    yield wrapper
                return
            except Exception as e:
                logger.warning(f"Error creating OTel/Phoenix span: {e}")

        # Fallback to structured logging span
        span = LoggingSpan(name, span_type, attrs)
        logger.debug(f"[Trace Start] {name} ({span_type}) -> {attrs}")
        try:
            yield span
        finally:
            logger.debug(f"[Trace End] {name} -> {span.attributes}")
