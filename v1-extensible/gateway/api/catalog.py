from typing import List
from fastapi import APIRouter, Depends, HTTPException
from core.models import AgentManifest
from gateway.services.catalog_service import CatalogService
from gateway.config import settings

router = APIRouter(prefix="/api/catalog", tags=["Catalog"])

_catalog_service = CatalogService(settings.catalog_path)


def get_catalog_service() -> CatalogService:
    return _catalog_service


@router.get("", response_model=List[AgentManifest])
def list_catalog(catalog: CatalogService = Depends(get_catalog_service)):
    """Returns the declarative catalog of available agents."""
    return catalog.list_manifests()


@router.get("/{agent_id}", response_model=AgentManifest)
def get_agent_manifest(agent_id: str, catalog: CatalogService = Depends(get_catalog_service)):
    manifest = catalog.get_manifest(agent_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found in catalog")
    return manifest
