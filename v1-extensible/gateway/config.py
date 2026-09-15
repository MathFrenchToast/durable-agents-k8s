import os
from pathlib import Path
from pydantic import BaseModel


class Settings(BaseModel):
    app_title: str = "Agent Control Plane & HITL Console"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    # Catalog
    catalog_path: Path = Path(os.getenv("CATALOG_PATH", "./catalog"))

    # Orchestrator
    orchestrator_type: str = os.getenv("ORCHESTRATOR_TYPE", "restate")  # "restate" | "dapr"
    restate_ingress_url: str = os.getenv(
        "RESTATE_INGRESS_URL", "http://restate.agent-system.svc.cluster.local:8080"
    )
    restate_admin_url: str = os.getenv(
        "RESTATE_ADMIN_URL", "http://restate.agent-system.svc.cluster.local:9070"
    )

    # Observability
    enable_phoenix: bool = os.getenv("ENABLE_PHOENIX", "false").lower() in ("true", "1")
    phoenix_url: str = os.getenv("PHOENIX_URL", "http://localhost:6006")


settings = Settings()
