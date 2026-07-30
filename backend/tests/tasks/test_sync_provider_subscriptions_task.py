from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.integrations.celery.tasks.register_provider_webhooks_task import (
    register_provider_webhooks,
    sync_provider_user_subscription,
)
from app.schemas.auth import LiveSyncMode
from app.services.providers.base_strategy import ProviderCapabilities, WebhookSubscriptionOwner


def _expected(**overrides: object) -> dict:
    """The task's one result shape, so a test states only what it cares about."""
    return {
        "provider": "",
        "owner": None,
        "dispatched": 0,
        "created": 0,
        "skipped": 0,
        "errors": 0,
        "reason": None,
        **overrides,
    }


_SYNC_USER = "app.integrations.celery.tasks.register_provider_webhooks_task.sync_provider_user_subscription"


def test_both_reconciliation_tasks_ack_only_after_completing() -> None:
    """The fan-out and the per-user task are equally load-bearing: losing either one
    silently leaves a user unreconciled until the next daily sweep, and for a
    connect-time dispatch that means no webhooks at all in the meantime."""
    for task in (register_provider_webhooks, sync_provider_user_subscription):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True


def test_fanout_dispatches_one_reconciliation_per_active_user() -> None:
    user_ids = [uuid4(), uuid4()]
    strategy = MagicMock()
    strategy.capabilities = ProviderCapabilities(
        rest_pull=True,
        webhook_ping=True,
        webhook_subscription_owner=WebhookSubscriptionOwner.USER,
    )
    strategy.webhook_service = MagicMock()
    strategy.connection_repo.get_all_active_by_provider.return_value = [
        MagicMock(user_id=user_id) for user_id in user_ids
    ]

    with (
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory,
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.SessionLocal") as session_local,
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.celery_app") as celery,
    ):
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        result = register_provider_webhooks.apply(args=["withings"]).get()

    assert result == _expected(provider="withings", owner=WebhookSubscriptionOwner.USER, dispatched=2)
    assert celery.send_task.call_count == 2
    dispatched_user_ids = {call.kwargs["args"][1] for call in celery.send_task.call_args_list}
    assert dispatched_user_ids == {str(user_id) for user_id in user_ids}
    assert all(call.args[0] == _SYNC_USER for call in celery.send_task.call_args_list)


def test_provider_without_a_subscription_owner_fails_loudly() -> None:
    strategy = MagicMock(webhook_service=None)
    strategy.capabilities = ProviderCapabilities()
    with patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory:
        factory.return_value.get_provider.return_value = strategy
        result = register_provider_webhooks.apply(args=["oura"]).get()
    assert result == _expected(provider="oura", errors=1, reason="unsupported")


def test_user_owned_provider_without_a_service_fails_loudly() -> None:
    strategy = MagicMock(webhook_service=None)
    strategy.capabilities = ProviderCapabilities(
        rest_pull=True,
        webhook_ping=True,
        webhook_subscription_owner=WebhookSubscriptionOwner.USER,
    )
    with patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory:
        factory.return_value.get_provider.return_value = strategy
        result = register_provider_webhooks.apply(args=["withings"]).get()
    assert result == _expected(provider="withings", errors=1, reason="unsupported")


def test_application_registration_uses_same_entry_task() -> None:
    service = MagicMock()
    service.register_subscriptions = AsyncMock(
        return_value=[
            {"status": "created"},
            {"status": "skipped"},
            {"status": "error"},
        ]
    )
    strategy = MagicMock(webhook_service=service)
    strategy.capabilities = ProviderCapabilities(webhook_subscription_owner=WebhookSubscriptionOwner.APPLICATION)

    with patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory:
        factory.return_value.get_provider.return_value = strategy
        result = register_provider_webhooks.apply(args=["oura", "https://example.test/webhook"]).get()

    service.register_subscriptions.assert_awaited_once_with("https://example.test/webhook")
    assert result == _expected(
        provider="oura",
        owner=WebhookSubscriptionOwner.APPLICATION,
        created=1,
        skipped=1,
        errors=1,
    )


@pytest.mark.parametrize("mode", [LiveSyncMode.WEBHOOK, LiveSyncMode.PULL])
def test_user_reconciliation_reads_latest_mode(mode: LiveSyncMode) -> None:
    user_id = uuid4()
    service = MagicMock()
    service.sync_user.return_value = [{"appli": 1, "status": "ok"}]
    strategy = MagicMock(webhook_service=service)
    strategy.effective_live_sync_mode.return_value = mode
    db = MagicMock()

    with (
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory,
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.SessionLocal") as session_local,
    ):
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=db)
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        result = sync_provider_user_subscription.apply(args=["withings", str(user_id)]).get()

    service.sync_user.assert_called_once_with(db, user_id, mode)
    assert result["mode"] == mode.value


def test_user_reconciliation_retries_deferred_work_on_the_providers_own_schedule() -> None:
    """A deferred result carries the wait the provider asked for; retrying on the flat
    default would re-collide with the same budget that just rejected us."""
    service = MagicMock()
    service.sync_user.return_value = [{"status": "deferred", "reason": "rate_limited", "retry_after": 9}]
    strategy = MagicMock(webhook_service=service)
    strategy.effective_live_sync_mode.return_value = LiveSyncMode.WEBHOOK

    with (
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory,
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.SessionLocal") as session_local,
        patch.object(sync_provider_user_subscription, "retry", side_effect=Exception("retry-called")) as retry,
    ):
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="retry-called"):
            sync_provider_user_subscription.apply(args=["withings", str(uuid4())]).get()

    assert retry.call_args.kwargs["countdown"] >= 9


def test_user_reconciliation_retries_partial_failure() -> None:
    service = MagicMock()
    service.sync_user.return_value = [
        {"appli": 1, "status": "subscribed"},
        {"appli": 44, "status": "error", "error": "boom"},
    ]
    strategy = MagicMock(webhook_service=service)
    strategy.effective_live_sync_mode.return_value = LiveSyncMode.WEBHOOK

    with (
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory") as factory,
        patch("app.integrations.celery.tasks.register_provider_webhooks_task.SessionLocal") as session_local,
        patch.object(sync_provider_user_subscription, "retry", side_effect=Exception("retry-called")),
    ):
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="retry-called"):
            sync_provider_user_subscription.apply(args=["withings", str(uuid4())]).get()
