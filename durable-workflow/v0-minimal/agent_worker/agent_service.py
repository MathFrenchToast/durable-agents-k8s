import asyncio
import os
import httpx
import restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")

# ==============================================================================
# 1. Premier Agent : Résolveur de tickets GitHub (méthode métier: resolve)
# ==============================================================================
github_issue_resolver = restate.VirtualObject("GitHubIssueResolver")


@github_issue_resolver.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    repo = payload.get("target_repo", "mon-orga/repo")
    issue_id = payload.get("issue_id", "42")

    # Étape d'analyse durable (idempotente & mémorisée par Restate)
    async def analyze():
        # Simulation du temps de réflexion LLM / inspection de code (2 secondes)
        await asyncio.sleep(2.0)
        return f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."

    solution = await ctx.run("analyze", analyze)

    # Point de suspension HITL (Human-in-the-Loop)
    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{GATEWAY_URL}/api/hitl/notify",
                json={
                    "instance_id": instance_id,
                    "agent_type": "GitHubIssueResolver",
                    "token": token,
                    "message": f"Analyse #{issue_id} sur {repo} terminée. Solution : {solution}",
                },
                timeout=10.0,
            )
        return True

    await ctx.run("notify_ui", notify)

    # HIBERNATION : libération immédiate du thread et du CPU
    decision = await promise

    # Reprise déterministe post-approbation
    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "PR_CREATED" if approved else "REJECTED",
        "comment": decision.get("feedback", "Approuvé" if approved else "Rejeté"),
    }


# ==============================================================================
# 2. Second Agent : Planificateur de réunions (méthode métier: schedule)
# ==============================================================================
meeting_scheduler = restate.VirtualObject("MeetingScheduler")


@meeting_scheduler.handler()
async def schedule(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    topic = payload.get("topic", "Point hebdo architecture")
    participants = payload.get("participants", "equipe@orga.com")

    # Étape durable de recherche de créneau
    async def find_slot():
        # Simulation de la négociation d'agendas / calendrier (2 secondes)
        await asyncio.sleep(2.0)
        return f"Créneau optimal trouvé pour '{topic}' ({participants}) : Jeudi 14h00-15h00."

    slot_proposal = await ctx.run("find_slot", find_slot)

    # Point de suspension HITL
    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{GATEWAY_URL}/api/hitl/notify",
                json={
                    "instance_id": instance_id,
                    "agent_type": "MeetingScheduler",
                    "token": token,
                    "message": f"Confirmation de réunion requise : {slot_proposal}",
                },
                timeout=10.0,
            )
        return True

    await ctx.run("notify_ui", notify)

    # HIBERNATION
    decision = await promise

    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "INVITATION_SENT" if approved else "CANCELLED",
        "comment": decision.get("feedback", "Réunion planifiée" if approved else "Annulée"),
    }


# ==============================================================================
# Enregistrement des deux agents dans un déploiement de worker commun
# ==============================================================================
app = restate.app(services=[github_issue_resolver, meeting_scheduler])
