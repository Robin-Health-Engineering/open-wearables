from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.integrations.celery.tasks.register_provider_webhooks_task import (
    register_provider_webhooks,
    sync_provider_user_subscription,
)
from app.services.providers.base_strategy import ProviderCapabilities

_FACTORY = "app.integrations.celery.tasks.register_provider_webhooks_task.ProviderFactory"
_SESSION = "app.integrations.celery.tasks.register_provider_webhooks_task.SessionLocal"


def test_both_reconciliation_tasks_ack_only_after_completing() -> None:
    """The entry task and the per-user task are equally load-bearing: losing either one
    silently leaves a user unreconciled, and for a connect-time dispatch that means no
    webhooks at all in the meantime."""
    for task in (register_provider_webhooks, sync_provider_user_subscription):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True


def test_registration_delegates_to_the_providers_webhook_service() -> None:
    service = MagicMock()
    service.register_subscriptions = AsyncMock(
        return_value=[{"status": "created"}, {"status": "skipped"}, {"status": "error"}]
    )
    strategy = MagicMock(webhook_service=service)
    strategy.capabilities = ProviderCapabilities(rest_pull=True, webhook_ping=True, webhook_registration_api=True)

    with patch(_FACTORY) as factory:
        factory.return_value.get_provider.return_value = strategy
        result = register_provider_webhooks.apply(args=["oura", "https://example.test/webhook"]).get()

    service.register_subscriptions.assert_awaited_once_with("https://example.test/webhook")
    assert result == {"provider": "oura", "created": 1, "skipped": 1, "errors": 1}


def test_registration_reports_a_provider_without_a_webhook_service() -> None:
    strategy = MagicMock(webhook_service=None)
    strategy.capabilities = ProviderCapabilities()

    with patch(_FACTORY) as factory:
        factory.return_value.get_provider.return_value = strategy
        result = register_provider_webhooks.apply(args=["oura", "https://example.test/webhook"]).get()

    assert result == {"provider": "oura", "created": 0, "skipped": 0, "errors": 1}


def test_user_reconciliation_delegates_to_the_providers_webhook_service() -> None:
    user_id = uuid4()
    service = MagicMock()
    service.reconcile_user_subscriptions.return_value = [{"appli": 1, "status": "subscribed"}]
    strategy = MagicMock(webhook_service=service)
    db = MagicMock()

    with patch(_FACTORY) as factory, patch(_SESSION) as session_local:
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=db)
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        result = sync_provider_user_subscription.apply(args=["withings", str(user_id)]).get()

    service.reconcile_user_subscriptions.assert_called_once_with(db, user_id)
    assert result == {
        "provider": "withings",
        "user_id": str(user_id),
        "results": [{"appli": 1, "status": "subscribed"}],
    }


def test_user_reconciliation_retries_deferred_work_on_the_providers_own_schedule() -> None:
    """A deferred result carries the wait the provider asked for; retrying on the flat
    default would re-collide with the same budget that just rejected us."""
    service = MagicMock()
    service.reconcile_user_subscriptions.return_value = [
        {"status": "deferred", "reason": "rate_limited", "retry_after": 9}
    ]
    strategy = MagicMock(webhook_service=service)

    with (
        patch(_FACTORY) as factory,
        patch(_SESSION) as session_local,
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
    service.reconcile_user_subscriptions.return_value = [
        {"appli": 1, "status": "subscribed"},
        {"appli": 44, "status": "error", "error": "boom"},
    ]
    strategy = MagicMock(webhook_service=service)

    with (
        patch(_FACTORY) as factory,
        patch(_SESSION) as session_local,
        patch.object(sync_provider_user_subscription, "retry", side_effect=Exception("retry-called")),
    ):
        factory.return_value.get_provider.return_value = strategy
        session_local.return_value.__enter__ = MagicMock(return_value=MagicMock())
        session_local.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="retry-called"):
            sync_provider_user_subscription.apply(args=["withings", str(uuid4())]).get()
