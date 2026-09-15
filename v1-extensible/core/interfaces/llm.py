from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: str  # "system", "user", "assistant"
    content: str


class LLMRequest(BaseModel):
    messages: List[LLMMessage]
    task_type: str = "general"  # e.g., "code_analysis", "summarization", "decision"
    model_hint: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 1024
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str
    model_used: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class BaseLLMRouter(ABC):
    """
    Abstract interface for LLM Routing.
    Enables pluggable implementations (LiteLLM, custom load balancers, fallback chains,
    or simulated offline mock).
    """

    @abstractmethod
    async def route_and_generate(self, request: LLMRequest) -> LLMResponse:
        """Route the prompt to the most suitable model/provider and generate completion."""
        pass
