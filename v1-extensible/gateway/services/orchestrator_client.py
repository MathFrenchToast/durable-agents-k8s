import logging
from abc import ABC, abstractmethod
from typing import Any, Dict
import httpx
from core.models import AgentManifest
from gateway.config import settings

logger = logging.getLogger("gateway.orchestrator")


class BaseOrchestratorClient(ABC):
    @abstractmethod
    async def dispatch_run(
        self,
        manifest: AgentManifest,
        instance_id: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def resolve_awakeable(
        self,
        token: str,
        approved: bool,
        feedback: str = "",
    ) -> Dict[str, Any]:
        pass


class RestateOrchestratorClient(BaseOrchestratorClient):
    """
    Communicates with Restate Server Ingress for dispatching and awakeable resolution.
    """

    def __init__(self, ingress_url: str):
        self.ingress_url = ingress_url.rstrip("/")

    async def dispatch_run(
        self,
        manifest: AgentManifest,
        instance_id: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        service = manifest.service_name
        handler = manifest.handler
        url = f"{self.ingress_url}/{service}/{instance_id}/{handler}"

        logger.info(f"Dispatching run to Restate: {url} with params: {params}")

        async with httpx.AsyncClient() as client:
            try:
                # Restate handles execution asynchronously.
                # In HTTP ingress, sending POST triggers the Virtual Object handler.
                res = await client.post(url, json=params, timeout=5.0)
                return {"status": "dispatched", "status_code": res.status_code, "url": url}
            except httpx.ReadTimeout:
                # Normal for long-running workflows handled asynchronously by Restate
                return {"status": "dispatched", "async": True}
            except Exception as e:
                logger.error(f"Error dispatching to Restate at {url}: {e}")
                raise

    async def resolve_awakeable(
        self,
        token: str,
        approved: bool,
        feedback: str = "",
    ) -> Dict[str, Any]:
        url = f"{self.ingress_url}/restate/awakeables/{token}/resolve"
        payload = {"approved": approved, "feedback": feedback}

        logger.info(f"Resolving awakeable on Restate: {url} with payload {payload}")

        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            if res.status_code >= 400:
                logger.error(f"Failed to resolve awakeable: HTTP {res.status_code} - {res.text}")
                raise RuntimeError(f"Restate awakeable resolution failed: HTTP {res.status_code}")
            return {"status": "resolved", "token": token}


class DaprOrchestratorClient(BaseOrchestratorClient):
    """
    Stub client for dispatching runs and raising events to Dapr Workflows / Actors.
    """

    def __init__(self, dapr_url: str = "http://localhost:3500"):
        self.dapr_url = dapr_url

    async def dispatch_run(
        self, manifest: AgentManifest, instance_id: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        logger.info(f"[Dapr Stub] Starting workflow {manifest.service_name} id={instance_id}")
        return {"status": "dispatched_dapr_stub", "instance_id": instance_id}

    async def resolve_awakeable(
        self, token: str, approved: bool, feedback: str = ""
    ) -> Dict[str, Any]:
        logger.info(f"[Dapr Stub] Resolving event {token}")
        return {"status": "resolved_dapr_stub", "token": token}


def get_orchestrator_client() -> BaseOrchestratorClient:
    if settings.orchestrator_type.lower() == "dapr":
        return DaprOrchestratorClient()
    return RestateOrchestratorClient(ingress_url=settings.restate_ingress_url)
