"""Celery tasks for registering provider webhook subscriptions.

Dispatched when a provider's live_sync_mode is switched in settings. Runs
asynchronously so the settings API response is not blocked. Providers with
per-user subscriptions fan out from their own ``register_subscriptions`` into
``sync_provider_user_subscription``, one task per active connection.
"""

import asyncio
import random
from logging import getLogger
from uuid import UUID

from celery import Task, shared_task

from app.database import SessionLocal
from app.integrations.celery.task_names import (
    REGISTER_PROVIDER_WEBHOOKS_TASK,
    SYNC_PROVIDER_USER_SUBSCRIPTION_TASK,
)
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.schemas.enums import ProviderName
from app.services.providers.factory import ProviderFactory
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(
    bind=True,
    name=REGISTER_PROVIDER_WEBHOOKS_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=60,
)
def register_provider_webhooks(self: Task, provider: str, callback_url: str) -> dict:
    """Register webhook subscriptions for a provider via its registration API.

    Only dispatched for providers with ``webhook_registration_api=True``.
    New subscriptions are created; existing ones are skipped.
    """
    try:
        strategy = ProviderFactory().get_provider(provider)
        if strategy.webhook_service is None:
            raise NotImplementedError(f"Provider '{provider}' does not support webhook subscription management")
        results = asyncio.run(strategy.webhook_service.register_subscriptions(callback_url))
        created = sum(1 for r in results if r.get("status") == "created")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        errors = sum(1 for r in results if r.get("status") == "error")
        log_structured(
            logger,
            "info",
            "Webhook subscriptions registered",
            provider=provider,
            action="register_provider_webhooks_complete",
            created=created,
            skipped=skipped,
            errors=errors,
        )
        if skipped and strategy.capabilities.webhook_inbound_secret:
            with SessionLocal() as db:
                secret = ProviderSettingsRepository().get_webhook_secret(db, ProviderName(provider))
            if not secret:
                log_structured(
                    logger,
                    "warning",
                    "Webhook skipped but no inbound secret stored — delete and re-register to obtain a new secret",
                    provider=provider,
                    action="webhook_inbound_secret_missing",
                )

        return {"provider": provider, "created": created, "skipped": skipped, "errors": errors}

    except (ValueError, NotImplementedError) as exc:
        log_structured(
            logger,
            "error",
            "Provider does not support webhook registration API",
            provider=provider,
            action="register_provider_webhooks_unsupported",
            error=str(exc),
        )
        return {"provider": provider, "created": 0, "skipped": 0, "errors": 1}
    except Exception as exc:
        log_structured(
            logger,
            "error",
            "Webhook registration task failed, scheduling retry",
            provider=provider,
            error=str(exc),
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        raise self.retry(exc=exc)


# Reconciling lists first and changes only the gap, so redelivery after a lost worker is safe.
@shared_task(
    bind=True,
    name=SYNC_PROVIDER_USER_SUBSCRIPTION_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=60,
)
def sync_provider_user_subscription(self: Task, provider: str, user_id: str) -> dict:
    """Reconcile one user's subscriptions with the provider's current live-sync mode."""
    strategy = ProviderFactory().get_provider(provider)
    service = strategy.webhook_service
    if service is None:
        raise NotImplementedError(f"Provider '{provider}' does not manage per-user webhook subscriptions")

    with SessionLocal() as db:
        results = service.reconcile_user_subscriptions(db, UUID(user_id))

    # Honour the wait the provider asked for, jittered: a fan-out rejected as a burst
    # would otherwise retry as the same burst.
    deferred = [result for result in results if result.get("status") == "deferred"]
    if deferred:
        wait = max(int(result.get("retry_after") or 0) for result in deferred)
        log_structured(
            logger,
            "info",
            "Provider user subscription reconciliation deferred",
            provider=provider,
            user_id=user_id,
            retry_after=wait,
            attempt=self.request.retries,
        )
        raise self.retry(
            exc=RuntimeError(f"{provider} subscription reconciliation deferred for user {user_id}"),
            countdown=wait + random.uniform(0, wait or 1),
        )

    failed = [result for result in results if result.get("status") == "error"]
    if failed:
        log_structured(
            logger,
            "error",
            "Provider user subscription reconciliation had failures",
            provider=provider,
            user_id=user_id,
            failed_items=failed,
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        # Attach the cause; a bare MaxRetriesExceededError would lose the failed items.
        raise self.retry(
            exc=RuntimeError(f"{provider} subscription reconciliation failed for user {user_id}: {failed}")
        )

    log_structured(
        logger,
        "info",
        "Provider user subscriptions reconciled",
        provider=provider,
        user_id=user_id,
        results=results,
    )
    return {"provider": provider, "user_id": user_id, "results": results}
