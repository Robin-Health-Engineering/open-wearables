"""Celery task for registering provider webhook subscriptions.

Dispatched when a provider's live_sync_mode is switched in settings.
Runs asynchronously so the settings API response is not blocked.

Application-owned subscriptions are registered once for the whole application;
user-owned subscriptions fan out to reconcile one subscription per active
connection.
"""

import asyncio
import random
from dataclasses import asdict, dataclass
from logging import getLogger
from typing import Any
from uuid import UUID

from celery import Task, shared_task
from celery import current_app as celery_app

from app.database import SessionLocal
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.schemas.auth import LiveSyncMode
from app.schemas.enums import ProviderName
from app.services.providers.base_strategy import BaseProviderStrategy, WebhookSubscriptionOwner
from app.services.providers.factory import ProviderFactory
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

REGISTER_PROVIDER_WEBHOOKS_TASK = (
    "app.integrations.celery.tasks.register_provider_webhooks_task.register_provider_webhooks"
)
SYNC_PROVIDER_USER_SUBSCRIPTION_TASK = (
    "app.integrations.celery.tasks.register_provider_webhooks_task.sync_provider_user_subscription"
)


@dataclass(frozen=True)
class WebhookReconciliationResult:
    """Represent a reconciliation outcome across subscription ownership models."""

    provider: str
    owner: WebhookSubscriptionOwner | None = None
    dispatched: int = 0
    created: int = 0
    skipped: int = 0
    errors: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fan_out_user_subscriptions(
    strategy: BaseProviderStrategy,
    provider: str,
    *,
    webhook_mode_only: bool,
) -> WebhookReconciliationResult:
    if strategy.webhook_service is None:
        raise NotImplementedError(f"Provider '{provider}' has no webhook subscription service")

    with SessionLocal() as db:
        if webhook_mode_only and strategy.effective_live_sync_mode(db) != LiveSyncMode.WEBHOOK:
            return WebhookReconciliationResult(
                provider=provider,
                owner=WebhookSubscriptionOwner.USER,
                reason="pull_mode",
            )
        connections = strategy.connection_repo.get_all_active_by_provider(db, provider)

    # The oldest active link owns each provider account's subscriptions, matching
    # inbound webhook attribution. A revoked grant yields ownership on the next sweep.
    subscription_owners: dict[tuple[str, str], str] = {}
    for connection in sorted(connections, key=lambda item: (item.created_at, str(item.id))):
        owner_key = (
            ("provider_user_id", connection.provider_user_id)
            if connection.provider_user_id is not None
            else ("connection_id", str(connection.id))
        )
        subscription_owners.setdefault(owner_key, str(connection.user_id))
    user_ids = list(subscription_owners.values())

    for user_id in user_ids:
        celery_app.send_task(SYNC_PROVIDER_USER_SUBSCRIPTION_TASK, args=[provider, user_id], queue="webhook_sync")

    log_structured(
        logger,
        "info",
        "Provider subscription sync fanned out",
        provider=provider,
        dispatched=len(user_ids),
    )
    return WebhookReconciliationResult(
        provider=provider,
        owner=WebhookSubscriptionOwner.USER,
        dispatched=len(user_ids),
    )


def _sync_application_subscriptions(
    strategy: BaseProviderStrategy,
    provider: str,
    callback_url: str | None,
) -> WebhookReconciliationResult:
    if strategy.webhook_service is None or callback_url is None:
        raise NotImplementedError(f"Provider '{provider}' has no application webhook registration service")

    results = asyncio.run(strategy.webhook_service.register_subscriptions(callback_url))
    created = sum(1 for result in results if result.get("status") == "created")
    skipped = sum(1 for result in results if result.get("status") == "skipped")
    errors = sum(1 for result in results if result.get("status") == "error")
    log_structured(
        logger,
        "info",
        "Provider webhook subscriptions reconciled",
        provider=provider,
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

    return WebhookReconciliationResult(
        provider=provider,
        owner=WebhookSubscriptionOwner.APPLICATION,
        created=created,
        skipped=skipped,
        errors=errors,
    )


@shared_task(
    bind=True,
    name=REGISTER_PROVIDER_WEBHOOKS_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=60,
)
def register_provider_webhooks(
    self: Task,
    provider: str,
    callback_url: str | None = None,
    *,
    webhook_mode_only: bool = False,
) -> dict:
    """Register webhook subscriptions for a provider via its registration API.

    Only dispatched for providers that declare a ``webhook_subscription_owner``.
    For ``APPLICATION`` owners new subscriptions are created and existing ones
    are skipped; for ``USER`` owners the work fans out over active connections,
    one subscription set per connection.
    """
    try:
        strategy = ProviderFactory().get_provider(provider)
        capabilities = strategy.capabilities
        if capabilities.webhook_subscription_owner == WebhookSubscriptionOwner.USER:
            return _fan_out_user_subscriptions(strategy, provider, webhook_mode_only=webhook_mode_only).to_dict()
        if capabilities.webhook_subscription_owner == WebhookSubscriptionOwner.APPLICATION:
            return _sync_application_subscriptions(strategy, provider, callback_url).to_dict()
        # Dispatched for a provider that declares no subscription owner: a wiring
        # bug upstream. Reporting zero work as success would hide it.
        raise NotImplementedError(f"Provider '{provider}' does not manage webhook subscriptions")
    except (ValueError, NotImplementedError) as exc:
        log_structured(
            logger,
            "error",
            "Provider does not support webhook subscription management",
            provider=provider,
            error=str(exc),
        )
        return WebhookReconciliationResult(provider=provider, errors=1, reason="unsupported").to_dict()
    except Exception as exc:
        log_structured(
            logger,
            "error",
            "Provider subscription reconciliation failed, scheduling retry",
            provider=provider,
            error=str(exc),
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        raise self.retry(exc=exc)


# Reconciling is idempotent — it lists first and changes only the gap — so redelivery is
# always safe, and losing this task instead leaves a user unreconciled until the next daily
# sweep. What actually recovers it is `acks_late` plus the broker's visibility timeout;
# `reject_on_worker_lost` only bites under a prefork pool and is inert while the worker runs
# `--pool=threads`, but it is declared so this task states the same intent as its entry task.
@shared_task(
    bind=True,
    name=SYNC_PROVIDER_USER_SUBSCRIPTION_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=60,
)
def sync_provider_user_subscription(self: Task, provider: str, user_id: str) -> dict:
    """Reconcile one user's subscriptions with the provider's current mode."""
    strategy = ProviderFactory().get_provider(provider)
    service = strategy.webhook_service
    if service is None:
        raise NotImplementedError(f"Provider '{provider}' does not manage per-user webhook subscriptions")

    with SessionLocal() as db:
        mode = strategy.effective_live_sync_mode(db)
        if mode is None:
            return {"provider": provider, "user_id": user_id, "mode": None, "results": []}
        results = service.sync_user(db, UUID(user_id), mode)

    # A provider that reports backpressure also reports how long to wait. Retrying on the
    # flat default would re-enter the same saturated budget that just turned us away, so
    # honour the wait and spread it: a fan-out is rejected as a burst and would otherwise
    # retry as the same burst.
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
            mode=mode.value,
            failed_items=failed,
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        # Retry with the cause attached: without it a terminal failure surfaces
        # as a bare MaxRetriesExceededError and the failed items are lost.
        raise self.retry(
            exc=RuntimeError(f"{provider} subscription reconciliation failed for user {user_id}: {failed}")
        )

    log_structured(
        logger,
        "info",
        "Provider user subscriptions reconciled",
        provider=provider,
        user_id=user_id,
        mode=mode.value,
        results=results,
    )
    return {"provider": provider, "user_id": user_id, "mode": mode.value, "results": results}
