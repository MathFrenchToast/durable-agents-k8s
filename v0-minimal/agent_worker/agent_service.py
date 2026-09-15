import os
import httpx
import restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")
agent_object = restate.VirtualObject("GitHubIssueResolver")


@agent_object.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    repo = payload.get("target_repo", "mon-orga/repo")
    issue_id = payload.get("issue_id", "42")

    # 1. Étape d'analyse durable (idempotente & mémorisée par Restate)
    async def analyze():
        return f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."

    solution = await ctx.run("analyze", analyze)

    # 2. Point de suspension HITL (Human-in-the-Loop)
    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{GATEWAY_URL}/api/hitl/notify",
                json={
                    "instance_id": instance_id,
                    "token": token,
                    "message": f"Analyse #{issue_id} sur {repo} terminée. Solution : {solution}",
                },
                timeout=10.0,
            )
        return True

    await ctx.run("notify_ui", notify)

    # HIBERNATION : libération immédiate du thread et du CPU
    decision = await promise

    # 3. Reprise déterministe post-approbation
    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "PR_CREATED" if approved else "REJECTED",
        "comment": decision.get("feedback", "Approuvé" if approved else "Rejeté"),
    }


app = restate.app(services=[agent_object])
