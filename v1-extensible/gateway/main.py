import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from gateway.config import settings
from gateway.api.catalog import router as catalog_router, get_catalog_service
from gateway.api.agents import router as agents_router
from gateway.api.hitl import router as hitl_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gateway.main")

app = FastAPI(
    title=settings.app_title,
    description="Application Chapeau: Orchestration Gateway & Console HITL pour agents durables",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Routers
app.include_router(catalog_router)
app.include_router(agents_router)
app.include_router(hitl_router)

# Template file path
TEMPLATE_FILE = Path(__file__).parent / "templates" / "index.html"


@app.on_event("startup")
async def on_startup():
    logger.info("Initializing Gateway application...")
    logger.info(f"Catalog Path: {settings.catalog_path.resolve()}")
    logger.info(f"Orchestrator Mode: {settings.orchestrator_type}")
    logger.info(f"Restate Ingress URL: {settings.restate_ingress_url}")

    catalog = get_catalog_service()
    catalog.reload()
    logger.info(f"Catalog initialized with {len(catalog.list_manifests())} agent(s).")


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    if TEMPLATE_FILE.exists():
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Console HITL - Template introuvable</h1>"


@app.get("/healthz")
def health_check():
    return {
        "status": "healthy",
        "orchestrator": settings.orchestrator_type,
        "agents_available": len(get_catalog_service().list_manifests()),
    }
