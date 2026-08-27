"""Authenticate Withings notifications, acknowledge them, and defer ingestion to Celery."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import compare_digest
from typing import Any, assert_never, final
from urllib.parse import parse_qs
from uuid import uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request, status
from pydantic import ValidationError

from app.config import settings
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.repositories.data_point_series_repository import WriteCounts
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.schemas.auth import LiveSyncMode
from app.schemas.providers.withings import WithingsNotification
from app.services import sync_status_service
from app.services.outgoing_webhooks.events import on_connection_revoked
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.withings.applis import (
    APPLI_DOMAIN,
    PROFILE_CHANGE_APPLI,
    PROFILE_CHANGE_REVOKING_ACTIONS,
    SUBSCRIBED_APPLIS,
    Domain,
)
from app.services.providers.withings.data_247 import Withings247Data
from app.services.providers.withings.results import WithingsUserWebhookResult
from app.services.providers.withings.webhook_dedup import claim_fetch
from app.services.providers.withings.workouts import WithingsWorkouts
from app.services.raw_payload_storage import store_raw_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"
_MAX_NOTIFY_WINDOW = timedelta(days=31)


@final
@dataclass(frozen=True)
class _ScreenedNotification:
    """A notification that cleared every inbound guard, with its fetch window resolved."""

    notification: WithingsNotification
    domain: Domain
    start: datetime
    end: datetime


@final
@dataclass(frozen=True)
class _ProfileChangeNotification:
    """An appli-46 notification whose action means we lost access upstream (delete/unlink)."""

    notification: WithingsNotification


class WithingsWebhookHandler(BaseWebhookHandler):
    user_id_field = "userid"

    def __init__(
        self,
        data_247: Withings247Data,
        workouts: WithingsWorkouts,
        default_live_sync_mode: LiveSyncMode | None = LiveSyncMode.PULL,
    ) -> None:
        super().__init__("withings")
        self.data_247 = data_247
        self.workouts = workouts  # appli 16 covers both daily activity and workouts
        self.connection_repo = UserConnectionRepository()
        self.provider_settings_repo = ProviderSettingsRepository()
        # Provider's default when no admin override is stored (Withings: PULL).
        self._default_live_sync_mode = default_live_sync_mode

    # ---------------------- inbound request handling ----------------------

    def parse_payload(self, body: bytes) -> dict[str, Any]:
        parsed = parse_qs(body.decode("utf-8"))
        return {k: v[0] for k, v in parsed.items()}

    @staticmethod
    def _has_valid_callback_token(request: Request) -> bool:
        expected = settings.withings_webhook_token
        actual = request.query_params.get("token")
        if expected is None or not actual:
            return False
        # compare_digest raises TypeError on non-ASCII str; the token is caller-supplied.
        return compare_digest(actual.encode("utf-8"), expected.get_secret_value().encode("utf-8"))

    def verify_signature(self, request: Request, body: bytes) -> bool:
        """Verify the callback token and require a userid-bearing notify body."""
        return self._has_valid_callback_token(request) and bool(self.parse_payload(body).get("userid"))

    def handle_probe(self, request: Request) -> None:
        """Accept an authenticated subscribe-time HEAD probe."""
        if not self._has_valid_callback_token(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Withings callback token")

    def supported_event_types(self) -> list[str]:
        return [str(appli) for appli in SUBSCRIBED_APPLIS]

    def _live_sync_mode_allows_webhook(self, db: DbSession) -> bool:
        configured = self.provider_settings_repo.get_live_sync_mode(db, self.provider_name)
        return (configured or self._default_live_sync_mode) == LiveSyncMode.WEBHOOK

    @staticmethod
    def _bounded_window(notification: WithingsNotification) -> tuple[datetime, datetime, str | None] | None:
        window = notification.resolve_window()
        if window is None:
            return None
        start, end = window
        if end < start:
            return start, end, "invalid_date_range"
        if end - start > _MAX_NOTIFY_WINDOW:
            return start, end, "date_range_too_large"
        return start, end, None

    def _screen(
        self, db: DbSession, payload: dict[str, Any]
    ) -> _ScreenedNotification | _ProfileChangeNotification | dict[str, Any]:
        """Validate a notification and resolve its domain, window, and current live-sync mode."""
        try:
            notification = WithingsNotification.model_validate(payload)
        except ValidationError:
            return {"status": "ignored", "reason": "invalid_payload_fields"}

        if notification.appli == PROFILE_CHANGE_APPLI:
            if notification.action not in PROFILE_CHANGE_REVOKING_ACTIONS:
                return {"status": "ignored", "reason": "profile_change", "action": notification.action}
            return _ProfileChangeNotification(notification=notification)

        domain = APPLI_DOMAIN.get(notification.appli)
        if domain is None:
            return {"status": "ignored", "reason": f"unhandled_appli: {notification.appli}"}

        bounded = self._bounded_window(notification)
        if bounded is None:
            return {"status": "ignored", "reason": "missing_date_range"}
        start, end, invalid_reason = bounded
        if invalid_reason:
            return {"status": "ignored", "reason": invalid_reason}

        if not self._live_sync_mode_allows_webhook(db):
            return {"status": "ignored", "reason": "live_sync_mode_not_webhook"}

        return _ScreenedNotification(notification=notification, domain=domain, start=start, end=end)

    def dispatch(self, db: DbSession, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the raw payload, then acknowledge fast and enqueue the data fetch
        (or revoke) on the ``webhook_sync`` queue."""
        trace_id = str(uuid4())[:8]
        store_raw_payload(source="webhook", provider="withings", payload=payload, trace_id=trace_id)

        screened = self._screen(db, payload)
        if isinstance(screened, dict):
            return screened

        userid = screened.notification.userid
        if isinstance(screened, _ProfileChangeNotification):
            known = bool(self.connection_repo.get_all_by_provider_user_id(db, "withings", userid))
        else:
            known = self.connection_repo.get_by_provider_user_id(db, "withings", userid) is not None
        if not known:
            return {"status": "ignored", "reason": "user_not_found", "withings_user_id": userid}

        celery_app.send_task(
            _PROCESS_PUSH_TASK,
            args=["withings", payload, trace_id],
            queue="webhook_sync",
        )
        return {"status": "accepted", "appli": screened.notification.appli}

    # ---------------------- async processing (Celery worker) ----------------------

    def process_payload(self, db: DbSession, payload: Any, trace_id: str) -> dict[str, Any]:
        """Fetch and persist the data referenced by a notification.

        Runs in the ``process_webhook_push`` worker with its own session. The
        payload is untrusted, so the guards are re-run via ``_screen`` and the
        user re-resolved from ``userid``.
        """
        screened = self._screen(db, payload)
        if isinstance(screened, dict):
            return screened

        if isinstance(screened, _ProfileChangeNotification):
            return self._revoke_local_connections(db, screened.notification, trace_id)

        connections = self.connection_repo.get_all_by_provider_user_id(db, "withings", screened.notification.userid)
        if not connections:
            return {"status": "user_not_found", "withings_user_id": screened.notification.userid}

        domain, start, end = screened.domain, screened.start, screened.end
        # Withings notifies once per category, so one event arrives several times
        # over the same window; only the first of them has anything to fetch. The
        # trace id is minted per notification and travels in the task payload, so
        # it tells a sibling apart from a redelivery of this very notification.
        with claim_fetch(
            withings_user_id=screened.notification.userid,
            domain=domain,
            start=start,
            end=end,
            notification_id=trace_id,
        ) as claimed:
            if not claimed:
                return {
                    "status": "ignored",
                    "reason": "duplicate_notification",
                    "appli": screened.notification.appli,
                    "domain": domain,
                }
            saved = 0
            user_ids: list[str] = []
            user_results: list[WithingsUserWebhookResult] = []
            for connection in connections:
                user_id = connection.user_id
                user_ids.append(str(user_id))
                components: dict[str, WriteCounts]
                if domain == "measures":
                    # appli 1/2/4/58 all fetch via getmeas (requested meastypes in coverage.py).
                    components = {"measures": WriteCounts.coerce(self.data_247.save_measures(db, user_id, start, end))}
                elif domain == "sleep":
                    components = {"sleep": WriteCounts.coerce(self.data_247.save_sleep(db, user_id, start, end))}
                elif domain == "activity_workouts":
                    # appli 16 covers both daily activity and workouts.
                    components = {
                        "activity": WriteCounts.coerce(self.data_247.save_activity(db, user_id, start, end)),
                        "workouts": WriteCounts.coerce(
                            self.workouts.load_data(
                                db,
                                user_id,
                                start_date=start.isoformat(),
                                end_date=end.isoformat(),
                            )
                        ),
                    }
                else:
                    assert_never(domain)

                user_result = WithingsUserWebhookResult(user_id=user_id, domain=domain, components=components)
                saved += user_result.items_processed
                user_results.append(user_result)

            # Emit only after the complete fan-out succeeds. A retry must not leave
            # terminal events for users processed before a later top-level failure.
            for user_result in user_results:
                sync_status_service.webhook_delivered(
                    str(user_result.user_id),
                    "withings",
                    status=user_result.status,
                    items_processed=user_result.items_processed,
                    message=f"Withings webhook processed {user_result.items_processed} items",
                    metadata=user_result.metadata(),
                )

        log_structured(
            logger,
            "info",
            "Withings webhook processed",
            provider="withings",
            appli=screened.notification.appli,
            domain=domain,
            user_ids=user_ids,
            items_processed=saved,
            trace_id=trace_id,
        )
        return {
            "status": "processed",
            "domain": domain,
            "records_saved": saved,
            "items_processed": saved,
            "user_ids": user_ids,
            "user_results": [user_result.to_dict() for user_result in user_results],
        }

    def _revoke_local_connections(
        self, db: DbSession, notification: WithingsNotification, trace_id: str
    ) -> dict[str, Any]:
        """Revoke every local connection for this Withings account — access was lost upstream.

        One Withings account can be linked to multiple OW users (multi-account
        fan-out); all of them lose access together, so all of them are revoked.
        """
        connections = self.connection_repo.get_all_by_provider_user_id(db, "withings", notification.userid)
        if not connections:
            return {"status": "user_not_found", "withings_user_id": notification.userid}

        user_ids = [str(connection.user_id) for connection in connections]
        for connection in connections:
            if self.connection_repo.disconnect(db, connection.user_id, "withings"):
                db.refresh(connection)
                on_connection_revoked(
                    user_id=connection.user_id,
                    provider="withings",
                    connection_id=connection.id,
                    reason=f"provider_{notification.action}",
                    revoked_at=connection.updated_at.isoformat(),
                )

        log_structured(
            logger,
            "info",
            "Withings profile change revoked local connections",
            provider="withings",
            action=notification.action,
            withings_user_id=notification.userid,
            user_ids=user_ids,
            trace_id=trace_id,
        )
        return {
            "status": "revoked",
            "action": notification.action,
            "withings_user_id": notification.userid,
            "user_ids": user_ids,
        }
