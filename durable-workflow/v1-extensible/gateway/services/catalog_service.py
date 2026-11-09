import logging
from pathlib import Path
from typing import Dict, List, Optional
import yaml
from core.models import AgentManifest

logger = logging.getLogger("gateway.catalog")


class CatalogService:
    def __init__(self, catalog_path: Path):
        self.catalog_path = catalog_path
        self._cache: Dict[str, AgentManifest] = {}

    def reload(self) -> None:
        self._cache.clear()
        if not self.catalog_path.exists():
            logger.warning(f"Catalog directory not found at {self.catalog_path}")
            return

        for manifest_file in self.catalog_path.glob("*.yaml"):
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    manifest = AgentManifest(**data)
                    self._cache[manifest.id] = manifest
                    logger.info(f"Loaded agent manifest: {manifest.id} ({manifest.name})")
            except Exception as e:
                logger.error(f"Failed to load manifest {manifest_file}: {e}")

    def list_manifests(self) -> List[AgentManifest]:
        if not self._cache:
            self.reload()
        return list(self._cache.values())

    def get_manifest(self, agent_id: str) -> Optional[AgentManifest]:
        if not self._cache:
            self.reload()
        return self._cache.get(agent_id)
