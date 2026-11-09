import asyncio
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from core.models import HITLItem, HITLNotification


class BaseHITLStore(ABC):
    @abstractmethod
    async def record_notification(self, notification: HITLNotification) -> None:
        pass

    @abstractmethod
    async def get_item(self, instance_id: str) -> Optional[HITLItem]:
        pass

    @abstractmethod
    async def list_pending(self) -> Dict[str, HITLItem]:
        pass

    @abstractmethod
    async def update_status(self, instance_id: str, status: str) -> bool:
        pass


class InMemoryHITLStore(BaseHITLStore):
    """
    Thread-safe in-memory store for Human-In-The-Loop notifications.
    Ideal for lightweight single-node MVP gateway.
    """

    def __init__(self):
        self._items: Dict[str, HITLItem] = {}
        self._lock = asyncio.Lock()

    async def record_notification(self, notification: HITLNotification) -> None:
        async with self._lock:
            self._items[notification.instance_id] = HITLItem(
                token=notification.token,
                message=notification.message,
                status="waiting",
                metadata=notification.metadata,
            )

    async def get_item(self, instance_id: str) -> Optional[HITLItem]:
        async with self._lock:
            return self._items.get(instance_id)

    async def list_pending(self) -> Dict[str, HITLItem]:
        async with self._lock:
            return {
                inst_id: item
                for inst_id, item in self._items.items()
                if item.status == "waiting"
            }

    async def update_status(self, instance_id: str, status: str) -> bool:
        async with self._lock:
            if instance_id in self._items:
                self._items[instance_id].status = status
                return True
            return False
