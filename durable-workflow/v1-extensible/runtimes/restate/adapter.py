from typing import Any, Awaitable, Callable, Tuple, TypeVar
from core.interfaces.runtime import ExecutionContext

T = TypeVar("T")


class RestateContextAdapter(ExecutionContext):
    """
    Adapter implementing ExecutionContext over Restate's ObjectContext.
    Enables durable execution, state persistence, checkpointing, and awakeables via Restate.
    """

    def __init__(self, restate_ctx: Any):
        self._ctx = restate_ctx

    def key(self) -> str:
        return self._ctx.key()

    async def run(self, step_name: str, action: Callable[..., Any]) -> T:
        """
        Delegates durable side-effect step checkpointing to Restate.
        Restate persists the result into its execution log.
        """
        import inspect

        async def _coro_action() -> T:
            res = action()
            if inspect.isawaitable(res):
                return await res
            return res

        return await self._ctx.run(step_name, _coro_action)

    def awakeable(self) -> Tuple[str, Awaitable[Any]]:
        """
        Creates an awakeable token and promise using Restate SDK.
        Execution suspends when the promise is awaited without holding active threads.
        """
        return self._ctx.awakeable()

    async def get_state(self, key: str) -> Any:
        return await self._ctx.get(key)

    async def set_state(self, key: str, value: Any) -> None:
        await self._ctx.set(key, value)

    async def clear_state(self, key: str) -> None:
        await self._ctx.clear(key)
