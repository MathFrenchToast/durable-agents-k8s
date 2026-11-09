import logging
from typing import Any, Callable, Dict, Optional
from core.interfaces.runtime import ExecutionContext
from core.interfaces.llm import BaseLLMRouter, LLMMessage, LLMRequest
from core.interfaces.telemetry import BaseTracer, NoOpTracer
from core.models import AgentExecutionResult

logger = logging.getLogger("agent.github_issue_resolver")


class GitHubIssueResolverAgent:
    """
    Pure domain implementation of the GitHub Issue Resolver Agent.
    Independent of execution runtime (Restate, Dapr) and LLM backend.
    """

    def __init__(
        self,
        llm_router: Optional[BaseLLMRouter] = None,
        tracer: Optional[BaseTracer] = None,
        notify_gateway_fn: Optional[Callable[[str, str, str], Any]] = None,
    ):
        self.llm_router = llm_router
        self.tracer = tracer or NoOpTracer()
        self.notify_gateway_fn = notify_gateway_fn

    async def execute_issue_analysis(self, repo: str, issue_id: str) -> str:
        """
        Simulate or execute issue analysis using the pluggable LLM router or tools.
        """
        async with self.tracer.start_span(
            "execute_issue_analysis",
            span_type="agent_step",
            attributes={"repo": repo, "issue_id": issue_id},
        ) as span:
            if self.llm_router:
                request = LLMRequest(
                    task_type="code_analysis",
                    messages=[
                        LLMMessage(
                            role="system",
                            content="Tu es un ingénieur logiciel expert. Analyse l'issue et propose un correctif précis.",
                        ),
                        LLMMessage(
                            role="user",
                            content=f"Analyse l'issue #{issue_id} sur le dépôt {repo}.",
                        ),
                    ],
                    metadata={"repo": repo, "issue_id": issue_id},
                )
                response = await self.llm_router.route_and_generate(request)
                analysis_result = response.content
                span.set_attribute("model_used", response.model_used)
            else:
                # Fallback / simulated deterministic response
                analysis_result = (
                    f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."
                )

            span.set_attribute("analysis_length", len(analysis_result))
            return analysis_result

    async def resolve(
        self,
        ctx: ExecutionContext,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Main execution workflow with durable steps and Human-in-the-Loop suspension.
        """
        instance_id = ctx.key()
        repo = payload.get("target_repo", "unknown/repo")
        issue_id = str(payload.get("issue_id", "0"))

        logger.info(f"[{instance_id}] Démarrage de l'agent GitHubIssueResolver pour {repo}#{issue_id}")

        async with self.tracer.start_span(
            "resolve_workflow",
            span_type="agent_step",
            attributes={"instance_id": instance_id, "repo": repo, "issue_id": issue_id},
        ) as span:

            import inspect

            # 1. Étape d'analyse durable (Idempotente & rejouable)
            async def run_analysis():
                return await self.execute_issue_analysis(repo, issue_id)

            solution = await ctx.run("analyze", run_analysis)
            span.set_attribute("solution", solution)

            # 2. Point de suspension HITL (Hibernation)
            awakeable_id, promise = ctx.awakeable()
            span.set_attribute("awakeable_id", awakeable_id)

            message = f"Analyse terminée pour #{issue_id} sur {repo}. Solution proposée : {solution}"

            # Notification durable de la gateway / console humaine
            if self.notify_gateway_fn:
                async def run_notify():
                    res = self.notify_gateway_fn(instance_id, message, awakeable_id)
                    if inspect.isawaitable(res):
                        return await res
                    return res

                await ctx.run("notify_ui", run_notify)

            logger.info(
                f"[{instance_id}] Entrée en hibernation HITL. Awakeable token : {awakeable_id}. Ressources libérées."
            )

            # SUSPENSION DU RUN : Libération immédiate des ressources sur le worker
            # Restate / Dapr suspend l'exécution ici jusqu'au réveil externe
            decision = await promise

            logger.info(f"[{instance_id}] Réveil post-arbitrage : decision={decision}")
            span.set_attribute("approved", decision.get("approved", False))

            # 3. Reprise post-approbation
            if decision.get("approved") is True:
                # Simulation d'action durable à effet de bord (création de PR)
                async def create_pr():
                    return f"PR #404 créée avec succès sur {repo} pour l'issue #{issue_id}"

                pr_info = await ctx.run("create_pull_request", create_pr)
                span.set_attribute("pr_info", pr_info)

                result = AgentExecutionResult(
                    status="completed",
                    instance=instance_id,
                    action="PR_CREATED",
                    comment=decision.get("feedback", "Approuvé sans modification"),
                    details={"pr_info": pr_info, "solution": solution},
                )
                return result.model_dump()

            result = AgentExecutionResult(
                status="aborted",
                instance=instance_id,
                action="REJECTED",
                comment=decision.get("feedback", "Rejeté par l'opérateur"),
                details={"solution": solution},
            )
            return result.model_dump()
