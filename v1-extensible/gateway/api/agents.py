import logging
from fastapi import APIRouter, Depends, HTTPException
from core.models import AgentLaunchRequest, AgentLaunchResponse
from gateway.api.catalog import get_catalog_service
from gateway.services.catalog_service import CatalogService
from gateway.services.orchestrator_client import (
    BaseOrchestratorClient,
    get_orchestrator_client,
)

router = APIRouter(prefix="/api/agents", tags=["Agents"])
logger = logging.getLogger("gateway.agents")


@router.post("/launch", response_model=AgentLaunchResponse)
async def launch_agent(
    body: AgentLaunchRequest,
    catalog: CatalogService = Depends(get_catalog_service),
    orchestrator: BaseOrchestratorClient = Depends(get_orchestrator_client),
):
    """
    Launches an agent instance using the configured durable orchestrator.
    """
    manifest = catalog.get_manifest(body.agent_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{body.agent_id}' non repertorie dans le catalogue.",
        )

    # Validate required parameters
    for param in manifest.parameters:
        if param.required and param.name not in body.params:
            if param.default is not None:
                body.params[param.name] = param.default
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Parametre requis manquant : '{param.name}'",
                )

    try:
        result = await orchestrator.dispatch_run(
            manifest=manifest,
            instance_id=body.instance_id,
            params=body.params,
        )
        return AgentLaunchResponse(
            status="dispatched",
            instance_id=body.instance_id,
            message=f"Agent '{manifest.name}' lance avec succes sur le moteur d'orchestration.",
        )
    except Exception as e:
        logger.error(f"Failed to dispatch agent run: {e}")
        raise HTTPException(status_code=500, detail=f"Echec du dispatch : {str(e)}")
