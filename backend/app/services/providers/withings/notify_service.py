"""Per-user Withings notify subscription reconciliation.

Withings subscriptions are created with the user's own bearer token, so there is
one set per active connection instead of a single application-level registration.
``register_subscriptions`` therefore fans out over active connections rather than
registering anything itself.
"""

import logging
from typing import Any
from uuid import UUID

from celery import current_app as celery_app
from pydantic import BaseModel, ValidationError

from app.database import DbSession, SessionLocal
from app.integrations.celery.task_names import SYNC_PROVIDER_USER_SUBSCRIPTION_TASK
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import LiveSyncMode
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.templates.base_webhook_service import BaseWebhookService
from app.services.providers.withings._client import withings_request
from app.services.providers.withings.applis import SUBSCRIBED_APPLIS
from app.services.providers.withings.callback import (
    MANAGED_COMMENT,
    WithingsCallbackUrlInvalidError,
    WithingsWebhookTokenUnconfiguredError,
    callback_endpoints_match,
    callback_urls_match,
    redact_callback_url,
    withings_callback_url,
)
from app.services.providers.withings.oauth import WithingsTokenError
from app.services.providers.withings.request_budget import WithingsRequestBudgetExceeded
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class WithingsNotifyProfile(BaseModel):
    """One profile returned by Withings Notify List."""

    appli: int
    callbackurl: str
    comment: str | None = None


class WithingsNotifyService(BaseWebhookService):
    """Reconciles a user's Withings notify subscriptions against the desired live-sync mode."""

    def __init__(
        self,
        connection_repo: UserConnectionRepository,
        oauth: BaseOAuthTemplate,
        default_live_sync_mode: LiveSyncMode | None = LiveSyncMode.PULL,
    ) -> None:
        self.connection_repo = connection_repo
        self.oauth = oauth
        self.provider_settings_repo = ProviderSettingsRepository()
        # Provider's default when no admin override is stored (Withings: PULL).
        self._default_live_sync_mode = default_live_sync_mode

    async def register_subscriptions(self, callback_url: str) -> list[dict[str, Any]]:
        """Fan out one reconciliation task per active connection.

        ``callback_url`` is ignored: each subscription carries the shared-secret
        callback built per request by ``withings_callback_url``.
        """
        with SessionLocal() as db:
            connections = self.connection_repo.get_all_active_by_provider(db, "withings")

        # The oldest active link owns each provider account's subscriptions, matching
        # inbound webhook attribution. A revoked grant yields ownership on the next fan-out.
        subscription_owners: dict[tuple[str, str], str] = {}
        for connection in sorted(connections, key=lambda item: (item.created_at, str(item.id))):
            owner_key = (
                ("provider_user_id", connection.provider_user_id)
                if connection.provider_user_id is not None
                else ("connection_id", str(connection.id))
            )
            subscription_owners.setdefault(owner_key, str(connection.user_id))

        results: list[dict[str, Any]] = []
        for user_id in subscription_owners.values():
            try:
                celery_app.send_task(
                    SYNC_PROVIDER_USER_SUBSCRIPTION_TASK,
                    args=["withings", user_id],
                    queue="webhook_sync",
                )
                results.append({"status": "dispatched", "user_id": user_id})
            except Exception as e:
                log_and_capture_error(
                    e,
                    logger,
                    "Withings subscription fan-out failed to dispatch",
                    extra={"provider": "withings", "user_id": user_id},
                )
                results.append({"status": "error", "user_id": user_id, "error": str(e)})

        log_structured(
            logger,
            "info",
            "Withings subscription sync fanned out",
            provider="withings",
            action="notify_fan_out",
            dispatched=sum(1 for result in results if result["status"] == "dispatched"),
        )
        return results

    def reconcile_user_subscriptions(self, db: DbSession, user_id: UUID) -> list[dict[str, Any]]:
        """Reconcile one user against the currently configured live-sync mode."""
        mode = self.provider_settings_repo.get_live_sync_mode(db, "withings") or self._default_live_sync_mode
        if mode is None:
            return [{"status": "skipped", "reason": "no_live_sync_mode"}]
        return self.sync_user(db, user_id, mode)

    def remove_user(self, db: DbSession, user_id: UUID) -> list[dict[str, Any]]:
        """Revoke a user's subscriptions on disconnect, data purge or account deletion.

        Subscriptions belong to the provider account rather than to one local profile,
        so a sibling profile still linked to it keeps them. Reconciling toward PULL
        prunes exactly the set this user owns.
        """
        if not self._is_last_active_link(db, user_id):
            return [{"status": "skipped", "reason": "provider_account_still_linked"}]
        return self.sync_user(db, user_id, LiveSyncMode.PULL)

    def _is_last_active_link(self, db: DbSession, user_id: UUID) -> bool:
        """Whether removing this connection leaves no active link to the same Withings account."""
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, "withings")
        if connection is None or connection.provider_user_id is None:
            return True
        linked = self.connection_repo.get_all_by_provider_user_id(db, "withings", connection.provider_user_id)
        return not any(other.user_id != user_id for other in linked)

    def sync_user(self, db: DbSession, user_id: UUID, mode: LiveSyncMode) -> list[dict[str, Any]]:
        """Reconcile the desired appli set without modifying foreign callback endpoints."""
        try:
            callback_url = withings_callback_url()
        except WithingsWebhookTokenUnconfiguredError:
            return [{"status": "skipped", "reason": "webhook_token_unconfigured"}]
        except WithingsCallbackUrlInvalidError as exc:
            # A misconfigured API_BASE_URL cannot be fixed by retrying, and this
            # runs once per user: skip cleanly instead of burning every retry.
            log_structured(
                logger,
                "warning",
                "Withings notify sync skipped: callback URL is not registrable",
                provider="withings",
                action="callback_url_invalid",
                user_id=str(user_id),
                error=str(exc),
            )
            return [{"status": "skipped", "reason": "callback_url_invalid"}]
        desired_applis = set(SUBSCRIBED_APPLIS) if mode == LiveSyncMode.WEBHOOK else set()
        try:
            existing = self._list_subscriptions(db, user_id)
        except WithingsTokenError as e:
            if e.invalid_grant:
                # An invalid grant is terminal until the user reconnects.
                log_structured(
                    logger,
                    "info",
                    "Withings notify sync skipped: refresh token invalid",
                    provider="withings",
                    user_id=str(user_id),
                )
                return [{"status": "skipped", "reason": "invalid_grant"}]
            log_and_capture_error(
                e,
                logger,
                "Withings notify list failed",
                extra={"provider": "withings", "user_id": str(user_id)},
            )
            return [{"status": "error", "error": str(e)}]
        except WithingsRequestBudgetExceeded as e:
            # Backpressure, not a fault: the application-wide budget is saturated and has
            # told us how long to wait. Reporting it as an error would page for a healthy
            # condition and retry on a schedule that ignores the wait it just handed us.
            log_structured(
                logger,
                "info",
                "Withings notify sync deferred: request budget exhausted",
                provider="withings",
                action="notify_sync_deferred",
                user_id=str(user_id),
                retry_after=e.retry_after_seconds,
            )
            return [{"status": "deferred", "reason": "rate_limited", "retry_after": e.retry_after_seconds}]
        except Exception as e:
            log_and_capture_error(
                e,
                logger,
                "Withings notify list failed",
                extra={"provider": "withings", "user_id": str(user_id)},
            )
            return [{"status": "error", "error": str(e)}]

        existing_by_appli: dict[int, list[WithingsNotifyProfile]] = {}
        for entry in existing:
            existing_by_appli.setdefault(entry.appli, []).append(entry)

        results: list[dict[str, Any]] = []
        active_desired_applis: set[int] = set()
        for appli in desired_applis:
            entries = existing_by_appli.get(appli, [])
            if any(callback_urls_match(entry.callbackurl, callback_url) for entry in entries):
                active_desired_applis.add(appli)
                results.append({"appli": appli, "status": "unchanged"})
                continue
            result = self._apply("subscribe", "subscribed", db, user_id, callback_url, appli)
            results.append(result)
            if result["status"] == "subscribed":
                active_desired_applis.add(appli)

        for appli, entries in existing_by_appli.items():
            for entry in entries:
                if appli in desired_applis and callback_urls_match(entry.callbackurl, callback_url):
                    continue
                if not callback_endpoints_match(entry.callbackurl, callback_url):
                    continue  # registered by a different host — not ours to touch
                if appli in desired_applis and appli not in active_desired_applis:
                    continue  # replacement failed; retain the old profile until a retry succeeds
                results.append(self._apply("revoke", "revoked", db, user_id, entry.callbackurl, appli))

        return results

    def _list_subscriptions(self, db: DbSession, user_id: UUID) -> list[WithingsNotifyProfile]:
        """List all applis and callback URLs in one request."""
        body = withings_request(
            db=db,
            user_id=user_id,
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            service_path="/notify",
            action="list",
            params={},
        )
        profiles: list[WithingsNotifyProfile] = []
        for raw_profile in body.get("profiles", []) or []:
            try:
                profiles.append(WithingsNotifyProfile.model_validate(raw_profile))
            except ValidationError as exc:
                callback_url = raw_profile.get("callbackurl") if isinstance(raw_profile, dict) else None
                log_structured(
                    logger,
                    "warning",
                    "Skipping invalid Withings notify profile",
                    provider="withings",
                    action="notify_profile_validation_failed",
                    user_id=str(user_id),
                    error=exc.errors(include_input=False),
                    callback_url=redact_callback_url(callback_url) if isinstance(callback_url, str) else None,
                )
        return profiles

    def _apply(
        self,
        action: str,
        ok_status: str,
        db: DbSession,
        user_id: UUID,
        callback_url: str,
        appli: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"callbackurl": callback_url, "appli": appli}
        if action == "subscribe":
            params["comment"] = MANAGED_COMMENT
        try:
            withings_request(
                db=db,
                user_id=user_id,
                connection_repo=self.connection_repo,
                oauth=self.oauth,
                service_path="/notify",
                action=action,
                params=params,
            )
            return {"appli": appli, "status": ok_status}
        except Exception as e:
            log_and_capture_error(
                e,
                logger,
                f"Withings {action} failed",
                extra={"provider": "withings", "appli": appli, "user_id": str(user_id)},
            )
            return {"appli": appli, "status": "error", "error": str(e)}
