from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Tuple, TypeVar

T = TypeVar("T")


class ExecutionContext(ABC):
    """
    Abstract interface for durable execution runtimes (Restate, Dapr, etc.).
    Decouples agent domain logic from any specific orchestrator or framework.
    """

    @abstractmethod
    def key(self) -> str:
        """Unique identifier of the current agent instance (e.g. Virtual Object key)."""
        pass

    @abstractmethod
    async def run(self, step_name: str, action: Callable[..., Awaitable[T]]) -> T:
        """
        Executes an idempotent, checkpointed side-effect step.
        If the worker restarts, Restate/Dapr replays the result without re-executing.
        """
        pass

    @abstractmethod
    def awakeable(self) -> Tuple[str, Awaitable[Any]]:
        """
        Generates an awakeable token and a suspension promise for Human-in-the-loop (HITL)
        or asynchronous webhook callbacks.
        Returns:
            (token_id, promise_awaitable)
        """
        pass

    @abstractmethod
    async def get_state(self, key: str) -> Any:
        """Retrieves persistent state for this instance."""
        pass

    @abstractmethod
    async def set_state(self, key: str, value: Any) -> None:
        """Sets persistent state for this instance."""
        pass

    @abstractmethod
    async def clear_state(self, key: str) -> None:
        """Clears persistent state for this instance."""
        pass
