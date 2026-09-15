from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional


class BaseSpan(ABC):
    @abstractmethod
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    @abstractmethod
    def record_exception(self, exception: Exception) -> None:
        pass


class BaseTracer(ABC):
    """
    Abstract tracing interface.
    Allows easy plugging of Arize Phoenix, OpenTelemetry, Datadog, or NoOp tracer.
    """

    @abstractmethod
    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        span_type: str = "agent_step",  # "agent_step", "llm", "tool", "hitl"
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[BaseSpan]:
        """Creates an async span for tracing operations."""
        pass


class NoOpSpan(BaseSpan):
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass


class NoOpTracer(BaseTracer):
    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        span_type: str = "agent_step",
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[BaseSpan]:
        yield NoOpSpan()
