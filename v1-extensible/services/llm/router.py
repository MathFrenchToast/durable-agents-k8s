import logging
import time
from typing import Any, Dict, Optional
from core.interfaces.llm import BaseLLMRouter, LLMRequest, LLMResponse

logger = logging.getLogger("llm.router")


class MockLLMRouter(BaseLLMRouter):
    """
    Mock LLM Router for local demos, offline testing, and CI/CD.
    Simulates model responses with token metrics without requiring external API keys.
    """

    def __init__(self, default_model: str = "mock-gpt-4o-mini"):
        self.default_model = default_model

    async def route_and_generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()
        logger.info(
            f"Routing prompt for task '{request.task_type}' (messages: {len(request.messages)})"
        )

        repo = request.metadata.get("repo", "unknown/repo")
        issue_id = request.metadata.get("issue_id", "42")

        if request.task_type == "code_analysis":
            content = (
                f"Analyse approfondie de {repo}#{issue_id} terminée :\n"
                f"- Cause racine : fuite de validation sur l'authentification token.\n"
                f"- Proposition : correctif sur src/security.py ligne 88 (ajout d'une vérification de validité).\n"
                f"- Tests unitaires associés recommandés."
            )
        else:
            content = f"Réponse synthétique pour la tâche '{request.task_type}'."

        latency_ms = (time.perf_counter() - start_time) * 1000 + 150  # simulate realistic latency
        prompt_tokens = sum(len(m.content.split()) * 2 for m in request.messages)
        completion_tokens = len(content.split()) * 2

        return LLMResponse(
            content=content,
            model_used=self.default_model,
            provider="mock-router",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=round(latency_ms, 2),
            raw_metadata={"task_type": request.task_type},
        )


class ConfigurableLLMRouter(BaseLLMRouter):
    """
    Pluggable router capable of routing between providers (LiteLLM, Ollama, OpenAI, Anthropic).
    When LiteLLM or an API provider is available, it routes requests according to configuration rules.
    Falls back gracefully to MockLLMRouter when offline or in lightweight demo mode.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.mock_fallback = MockLLMRouter()

    async def route_and_generate(self, request: LLMRequest) -> LLMResponse:
        routing_rules = self.config.get("routing_rules", {})
        target_model = routing_rules.get(request.task_type, self.config.get("default_model", "mock"))

        # If configured for an external provider and litellm is installed:
        if target_model != "mock":
            try:
                import litellm

                response = await litellm.acompletion(
                    model=target_model,
                    messages=[{"role": m.role, "content": m.content} for m in request.messages],
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                )
                choice = response.choices[0].message
                usage = getattr(response, "usage", None)
                return LLMResponse(
                    content=choice.content,
                    model_used=target_model,
                    provider="litellm-router",
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                    total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
                )
            except ImportError:
                logger.debug("LiteLLM not installed. Falling back to MockLLMRouter.")
            except Exception as e:
                logger.warning(f"Error during external LLM call ({target_model}): {e}. Using fallback.")

        return await self.mock_fallback.route_and_generate(request)
