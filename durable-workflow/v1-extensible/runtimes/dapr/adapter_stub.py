import uuid
from typing import Any, Awaitable, Callable, Tuple, TypeVar
from core.interfaces.runtime import ExecutionContext

T = TypeVar("T")


class DaprContextAdapter(ExecutionContext):
    """
    Adapter stub demonstrating how Dapr Workflows / Actors satisfy the ExecutionContext contract.
    Ready for future implementation using 'dapr-ext-workflow' or Dapr Actors.
    """

    def __init__(self, instance_id: str, dapr_client: Any = None):
        self._instance_id = instance_id
        self._client = dapr_client
        self._mock_state: dict = {}

    def key(self) -> str:
        return self._instance_id

    async def run(self, step_name: str, action: Callable[..., Awaitable[T]]) -> T:
        """
        In Dapr Workflow, maps to:
        return await self._workflow_ctx.call_activity(step_name, input=...)
        """
        # Executing action
        return await action()

    def awakeable(self) -> Tuple[str, Awaitable[Any]]:
        """
        In Dapr Workflow, maps to waiting for an external event:
        await self._workflow_ctx.wait_for_external_event(event_name)
        """
        token = f"dapr-awakeable-{uuid.uuid4().hex[:12]}"

        # Stub future/promise representing the external Dapr event
        import asyncio

        future = asyncio.get_event_loop().create_future()

        # To resolve in Dapr:
        # POST /v1.0-alpha1/workflows/<workflow>/<instance_id>/raise-event/token
        return token, future

    async def get_state(self, key: str) -> Any:
        if self._client:
            # resp = await self._client.get_state(store_name="statestore", key=f"{self._instance_id}:{key}")
            # return resp.data
            pass
        return self._mock_state.get(key)

    async def set_state(self, key: str, value: Any) -> None:
        if self._client:
            # await self._client.save_state(store_name="statestore", key=f"{self._instance_id}:{key}", value=value)
            pass
        self._mock_state[key] = value

    async def clear_state(self, key: str) -> None:
        if self._client:
            # await self._client.delete_state(store_name="statestore", key=f"{self._instance_id}:{key}")
            pass
        self._mock_state.pop(key, None)
