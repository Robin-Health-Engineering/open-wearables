"""``withings.reading.created``: tell Robin a purchased device recorded a new reading.

The one place OW calls robin-backend (spec: "OW -> Robin signed event", a documented exception to
Robin's no-callback rule). It carries ids and metric NAMES only, never values; Robin reads the
reading back through the readings API, authenticated as it always is.

Sent only for groups that are
* NEW, i.e. just inserted into withings_measure_group (``record_new_groups``);
* on a connection WE provisioned (a ``withings_sdk_account`` row), because a member's own
  Withings account is not the device we sold them;
* device-captured (a ``deviceid``, attrib not 2/4 = manual entry);
* carrying at least one C2 metric (a blood-pressure-only group has nothing to announce);
* RECENT (``robin_reading_event_max_age_hours``), because backfills re-read history.

This module stops at the enqueue. Signing and the POST are the Celery task's (``DELIVER_TASK``),
enqueued by name so there is no import dependency on it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from celery import current_app as celery_app

from app.config import settings
from app.database import DbSession
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings.measure_groups import ParsedGroup
from app.services.providers.withings.sdk_devices import sync_devices_from_withings
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

EVENT_NAME = "withings.reading.created"
DELIVER_TASK = "app.integrations.celery.tasks.withings_reading_event_task.deliver_withings_reading_event"
_MANUAL_ATTRIBS = frozenset({2, 4})


def is_enabled() -> bool:
    return bool(settings.robin_reading_event_url) and settings.robin_reading_event_secret is not None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(
    *, external_user_id: str, withings_user_id: str | None, group: ParsedGroup, hash_deviceid: str | None = None
) -> dict[str, Any]:
    return {
        "event": EVENT_NAME,
        "external_user_id": external_user_id,
        "withings_user_id": withings_user_id,
        "device_id": group.device_id,
        "hash_deviceid": hash_deviceid,
        "grpid": group.grpid,
        "measured_at": _iso_utc(group.measured_at),
        "types": list(group.metric_keys),
    }


def _eligible(group: ParsedGroup, cutoff: datetime) -> bool:
    return (
        group.device_id is not None
        and group.attrib not in _MANUAL_ATTRIBS
        and group.has_c2_metrics
        and group.measured_at >= cutoff
    )


def _devices(db: DbSession, user_connection_id: UUID, device_ids: set[str]) -> dict[str, tuple[str | None, bool]]:
    """device_id -> (stored hash, whether Getdevice has ever listed it). Absent = no row yet."""
    rows = db.query(WithingsDevice.device_id, WithingsDevice.hash_device_id, WithingsDevice.last_getdevice_at).filter(
        WithingsDevice.user_connection_id == user_connection_id, WithingsDevice.device_id.in_(device_ids)
    )
    return {device_id: (hash_device_id, listed_at is not None) for device_id, hash_device_id, listed_at in rows}


def _needs_refresh(devices: dict[str, tuple[str | None, bool]], device_ids: set[str]) -> bool:
    """A device with no row, or a row Getdevice has never listed, and no hash.

    A device Getdevice HAS listed without a hash simply does not report one: asking again on
    every reading would cost a Getdevice call per weigh-in and change nothing.
    """
    for device_id in device_ids:
        hash_device_id, listed = devices.get(device_id, (None, False))
        if hash_device_id is None and not listed:
            return True
    return False


def _rollback(db: DbSession, user_connection_id: UUID) -> None:
    """Leave the caller's session usable; a failing rollback must not escape either."""
    try:
        db.rollback()
    except Exception as rollback_error:
        log_and_capture_error(
            rollback_error,
            logger,
            "Withings reading event rollback failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )


def _refresh_hashes(db: DbSession, *, user_id: UUID, user_connection_id: UUID, oauth: BaseOAuthTemplate) -> None:
    """One Getdevice sweep of this connection, so a first reading can carry its hash_deviceid.

    A best effort: on failure the event still goes out with a null hash (Robin then falls back to
    the provisioned-account check), so this logs and swallows.
    """
    try:
        sync_devices_from_withings(db, user_id=user_id, oauth=oauth, connection_id=user_connection_id)
    except Exception as e:
        _rollback(db, user_connection_id)
        log_and_capture_error(
            e,
            logger,
            "Withings Getdevice refresh for a reading event failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )


def enqueue_new_reading_events(
    db: DbSession,
    *,
    user_connection_id: UUID,
    groups: list[ParsedGroup],
    now: datetime | None = None,
    oauth: BaseOAuthTemplate | None = None,
) -> int:
    """Enqueue one delivery per eligible group. Never raises: the samples are already committed.

    ``oauth`` enables the Getdevice refresh for a device whose hash is not stored yet, run at most
    once per call. Without it the hash is whatever is stored, possibly null.
    """
    if not groups or not is_enabled():
        return 0
    try:
        account = (
            db.query(WithingsSdkAccount)
            .filter(WithingsSdkAccount.user_connection_id == user_connection_id)
            .one_or_none()
        )
        if account is None:
            return 0
        connection = UserConnectionRepository().get(db, user_connection_id)
        if connection is None:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=settings.robin_reading_event_max_age_hours)
        eligible = [g for g in groups if _eligible(g, cutoff)]
        if not eligible:
            return 0
        # Read everything off the ORM objects now: a failed refresh rolls the session back.
        external_user_id = account.external_id
        # JSON null rather than the string "None" when Withings never reported a userid.
        withings_user_id = str(connection.provider_user_id) if connection.provider_user_id is not None else None
        user_id = connection.user_id
        device_ids = {g.device_id for g in eligible if g.device_id is not None}

        devices = _devices(db, user_connection_id, device_ids)
        if oauth is not None and _needs_refresh(devices, device_ids):
            # The first reading from a device can beat the Getdevice sweep that stores its hash.
            _refresh_hashes(db, user_id=user_id, user_connection_id=user_connection_id, oauth=oauth)
            devices = _devices(db, user_connection_id, device_ids)
        hashes = {device_id: devices.get(device_id, (None, False))[0] for device_id in device_ids}
    except Exception as e:
        _rollback(db, user_connection_id)
        log_and_capture_error(
            e,
            logger,
            "Withings reading event enqueue failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )
        return 0

    sent = 0
    for group in eligible:
        # Per group: the groups are already committed as seen, so one failed enqueue must not
        # drop the rest of the batch with it.
        try:
            payload = build_payload(
                external_user_id=external_user_id,
                withings_user_id=withings_user_id,
                group=group,
                hash_deviceid=hashes.get(group.device_id or ""),
            )
            celery_app.send_task(DELIVER_TASK, args=[payload], queue="default")
            sent += 1
        except Exception as e:
            log_and_capture_error(
                e,
                logger,
                "Withings reading event enqueue failed",
                extra={"provider": "withings", "user_connection_id": str(user_connection_id), "grpid": group.grpid},
            )
    log_structured(
        logger,
        "info",
        "Withings reading events enqueued",
        provider="withings",
        action="reading_event_enqueued",
        user_connection_id=str(user_connection_id),
        count=sent,
        failed=len(eligible) - sent,
        without_hash=sum(1 for h in hashes.values() if h is None),
    )
    return sent
