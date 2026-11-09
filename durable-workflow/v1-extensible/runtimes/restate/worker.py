import logging
import os
import httpx
import restate
from restate import ObjectContext

from core.agents.github_issue_resolver import GitHubIssueResolverAgent
from runtimes.restate.adapter import RestateContextAdapter
from services.observability.phoenix import setup_phoenix_tracing
from services.llm.router import ConfigurableLLMRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker.restate")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau.agent-system.svc.cluster.local:8000")

# Telemetry and LLM router initialization
tracer = setup_phoenix_tracing(service_name="agent-worker-restate")
llm_router = ConfigurableLLMRouter()


async def notify_hitl_gateway(instance_id: str, message: str, token: str):
    """
    Side-effect executed via ctx.run() to notify the human-in-the-loop dashboard.
    """
    payload = {
        "instance_id": instance_id,
        "token": token,
        "message": message,
        "metadata": {"runtime": "restate", "service": "GitHubIssueResolver"},
    }
    url = f"{GATEWAY_URL}/api/hitl/notify"
    logger.info(f"Notifying Gateway at {url} for instance {instance_id}")

    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, timeout=10.0)
        res.raise_for_status()
        return {"notified": True, "instance_id": instance_id}


# Domain Agent instance
agent = GitHubIssueResolverAgent(
    llm_router=llm_router,
    tracer=tracer,
    notify_gateway_fn=notify_hitl_gateway,
)

# Restate Virtual Object definition
agent_object = restate.VirtualObject("GitHubIssueResolver")


@agent_object.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    """
    Restate Virtual Object handler for GitHubIssueResolver.
    Wraps ObjectContext with RestateContextAdapter and executes the pure domain agent.
    """
    adapter = RestateContextAdapter(ctx)
    return await agent.resolve(adapter, payload)


# Restate ASGI app
app = restate.app(services=[agent_object])
