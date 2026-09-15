import asyncio
import pytest
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from core.interfaces.runtime import ExecutionContext
from core.agents.github_issue_resolver import GitHubIssueResolverAgent
from services.llm.router import MockLLMRouter


class MockExecutionContext(ExecutionContext):
    """
    In-memory test execution context simulating a durable execution runtime.
    Validates that the domain agent can be unit-tested without Restate, Dapr, or Docker!
    """

    def __init__(self, key: str, simulated_decision: Dict[str, Any]):
        self._key = key
        self.simulated_decision = simulated_decision
        self.recorded_steps: List[str] = []
        self.state: Dict[str, Any] = {}
        self.awakeable_token: str = "test-token-12345"

    def key(self) -> str:
        return self._key

    async def run(self, step_name: str, action: Callable[..., Awaitable[Any]]) -> Any:
        self.recorded_steps.append(step_name)
        return await action()

    def awakeable(self) -> Tuple[str, Awaitable[Any]]:
        async def resolve_future():
            return self.simulated_decision

        return self.awakeable_token, resolve_future()

    async def get_state(self, key: str) -> Any:
        return self.state.get(key)

    async def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    async def clear_state(self, key: str) -> None:
        self.state.pop(key, None)


@pytest.mark.asyncio
async def test_agent_approval_flow():
    notified = []

    async def fake_notify(instance_id: str, msg: str, token: str):
        notified.append((instance_id, msg, token))

    agent = GitHubIssueResolverAgent(
        llm_router=MockLLMRouter(),
        notify_gateway_fn=fake_notify,
    )

    ctx = MockExecutionContext(
        key="test-instance-1",
        simulated_decision={"approved": True, "feedback": "Code approved by reviewer"},
    )

    result = await agent.resolve(ctx, {"target_repo": "test/repo", "issue_id": "42"})

    assert result["status"] == "completed"
    assert result["action"] == "PR_CREATED"
    assert "PR #404" in result["details"]["pr_info"]
    assert "analyze" in ctx.recorded_steps
    assert "create_pull_request" in ctx.recorded_steps
    assert len(notified) == 1
    assert notified[0][0] == "test-instance-1"


@pytest.mark.asyncio
async def test_agent_rejection_flow():
    agent = GitHubIssueResolverAgent(llm_router=MockLLMRouter())

    ctx = MockExecutionContext(
        key="test-instance-2",
        simulated_decision={"approved": False, "feedback": "Needs rework"},
    )

    result = await agent.resolve(ctx, {"target_repo": "test/repo", "issue_id": "99"})

    assert result["status"] == "aborted"
    assert result["action"] == "REJECTED"
    assert "analyze" in ctx.recorded_steps
    assert "create_pull_request" not in ctx.recorded_steps
