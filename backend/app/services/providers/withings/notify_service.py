"""Per-user Withings notify subscription reconciliation.

Withings subscriptions are created with the user's bearer token, so they are
managed per-user rather than through the app-level webhook service.
"""

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.database import DbSession
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

    def __init__(self, connection_repo: UserConnectionRepository, oauth: BaseOAuthTemplate) -> None:
        self.connection_repo = connection_repo
        self.oauth = oauth

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
