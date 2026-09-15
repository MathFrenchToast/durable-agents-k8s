import logging
from typing import Dict
from fastapi import APIRouter, Depends, HTTPException
from core.models import HITLItem, HITLNotification, HITLResolution
from gateway.services.hitl_store import BaseHITLStore, InMemoryHITLStore
from gateway.services.orchestrator_client import (
    BaseOrchestratorClient,
    get_orchestrator_client,
)

router = APIRouter(prefix="/api/hitl", tags=["Human-in-the-Loop"])
logger = logging.getLogger("gateway.hitl")

# Singleton store instance for MVP
_hitl_store = InMemoryHITLStore()


def get_hitl_store() -> BaseHITLStore:
    return _hitl_store


@router.post("/notify")
async def receive_notification(
    body: HITLNotification,
    store: BaseHITLStore = Depends(get_hitl_store),
):
    """
    Called by an agent worker when entering hibernation / awaiting human decision.
    """
    logger.info(
        f"Received HITL notification for instance {body.instance_id} (token={body.token})"
    )
    await store.record_notification(body)
    return {"status": "stored", "instance_id": body.instance_id}


@router.get("/pending", response_model=Dict[str, HITLItem])
async def list_pending(store: BaseHITLStore = Depends(get_hitl_store)):
    """
    Lists all agent instances currently hibernating and waiting for operator interaction.
    """
    return await store.list_pending()


@router.post("/resolve/{instance_id}")
async def resolve_interaction(
    instance_id: str,
    body: HITLResolution,
    store: BaseHITLStore = Depends(get_hitl_store),
    orchestrator: BaseOrchestratorClient = Depends(get_orchestrator_client),
):
    """
    Called by human operator from the UI to approve or reject the agent's proposal.
    Triggers awakeable resolution on the orchestrator (Restate / Dapr).
    """
    item = await store.get_item(instance_id)
    if not item or item.status != "waiting":
        raise HTTPException(
            status_code=404,
            detail=f"Aucun signal d'arbitrage en attente pour l'instance '{instance_id}'",
        )

    token = item.token
    logger.info(
        f"Resolving HITL interaction for {instance_id}: approved={body.approved}, token={token}"
    )

    try:
        await orchestrator.resolve_awakeable(
            token=token,
            approved=body.approved,
            feedback=body.feedback,
        )
        await store.update_status(instance_id, "resolved")
        return {
            "status": "success",
            "instance_id": instance_id,
            "approved": body.approved,
        }
    except Exception as e:
        logger.error(f"Failed to resolve awakeable on orchestrator: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Echec de la resolution sur le plan de controle : {str(e)}",
        )
