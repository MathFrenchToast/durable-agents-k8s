from pathlib import Path
import pytest
from core.models import HITLNotification
from gateway.services.catalog_service import CatalogService
from gateway.services.hitl_store import InMemoryHITLStore


def test_catalog_loading():
    catalog_path = Path(__file__).parent.parent / "catalog"
    service = CatalogService(catalog_path)
    manifests = service.list_manifests()

    assert len(manifests) >= 1
    resolver = service.get_manifest("github-issue-resolver")
    assert resolver is not None
    assert resolver.service_name == "GitHubIssueResolver"
    assert resolver.handler == "resolve"
    assert any(p.name == "target_repo" for p in resolver.parameters)


@pytest.mark.asyncio
async def test_hitl_store_lifecycle():
    store = InMemoryHITLStore()

    # Initial pending list should be empty
    pending = await store.list_pending()
    assert len(pending) == 0

    # Add notification
    notification = HITLNotification(
        instance_id="test-inst-1",
        token="token-abc",
        message="Awaiting approval",
    )
    await store.record_notification(notification)

    # Verify pending
    pending = await store.list_pending()
    assert "test-inst-1" in pending
    assert pending["test-inst-1"].status == "waiting"

    # Resolve
    updated = await store.update_status("test-inst-1", "resolved")
    assert updated is True

    # Check pending again
    pending = await store.list_pending()
    assert "test-inst-1" not in pending
