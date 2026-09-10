"""A member's Withings devices, as reported by Withings and as the member is shown them.

Two writers, and the reconciliation between them:

* an **install-success notification**, which only the app ever sees, and
* **``User v2 - Getdevice``**, a token-authenticated call only the server can make.

Getdevice is the authority and the only one that survives an app reinstall; the notification
merely gets a device row in sooner, since Getdevice may not list a just-installed device yet.

This module used to exist for ``advertise_key`` — the per-device token the Withings Mobile SDK
needed to start background BLE sync, which Withings requires be collected from both sources.
That integration is abandoned (cellular devices ship already connected, so there is nothing to
pair) and the column is gone. The rule it motivated survives and still matters: **a write never
erases what it cannot replace.** Getdevice shapes its response by what each device reports, so
an entry omitting ``battery`` means "this device did not say", not "there is no battery level".

Reference: https://developer.withings.com/api-reference/#tag/devices
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from app.database import DbSession
from app.models.user_connection import UserConnection
from app.models.withings_device import WithingsDevice
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.providers.withings.devices import WithingsDeviceEntry, WithingsGetdeviceBody
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings._client import WITHINGS_API_BASE_URL, withings_request
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Which writer produced an upsert. It is no longer stored — the column it fed
# (``advertise_key_source``) went with the Mobile SDK — but it still decides whether a write
# stamps ``last_getdevice_at``, which is what keeps the dissociation sweep from retiring a
# device Withings' own list has simply not caught up with yet.
#
# A Literal rather than a bare str so every call site is checked.
DeviceWriteSource = Literal["notification", "getdevice"]

SOURCE_NOTIFICATION: DeviceWriteSource = "notification"
SOURCE_GETDEVICE: DeviceWriteSource = "getdevice"


class WithingsDeviceError(RuntimeError):
    """Raised when a device operation cannot be attributed to a Withings connection."""


def _connection(db: DbSession, user_id: UUID) -> UserConnection:
    """The member's one Withings connection, or an error naming why there is none."""
    connection = UserConnectionRepository().get_by_user_and_provider(db, user_id, ProviderName.WITHINGS.value)
    if connection is None:
        raise WithingsDeviceError("this member has no Withings connection")
    return connection


def _from_unix(seconds: int | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds else None


def _upsert(
    db: DbSession,
    *,
    connection_id: UUID,
    device_id: str,
    source: DeviceWriteSource,
    model_id: int | None = None,
    model: str | None = None,
    device_type: str | None = None,
    battery: str | None = None,
    last_session_at: datetime | None = None,
) -> WithingsDevice:
    """Create or update one device row, without ever losing what an earlier write stored.

    Every optional field is written only when a value is actually supplied. Getdevice shapes its
    response by what a device reports, so an entry omitting ``battery`` means "this device did
    not say" — not "the battery is unknown now" — and must leave the stored value alone.
    """
    now = datetime.now(timezone.utc)
    existing = (
        db.query(WithingsDevice)
        .filter(
            WithingsDevice.user_connection_id == connection_id,
            WithingsDevice.device_id == device_id,
        )
        .one_or_none()
    )

    device = existing or WithingsDevice(
        id=uuid4(),
        user_connection_id=connection_id,
        device_id=device_id,
    )

    if model_id is not None:
        device.model_id = model_id
    if model:
        device.model = model
    if device_type:
        device.device_type = device_type
    if battery:
        device.battery = battery
    if last_session_at is not None:
        device.last_session_at = last_session_at

    # Stamped for a Getdevice write and ONLY a Getdevice write — it records that Withings' own
    # list has seen this device, which is what makes the dissociation sweep safe. A device
    # Getdevice has never listed says nothing by being absent from it.
    if source == SOURCE_GETDEVICE:
        device.last_getdevice_at = now

    # Seeing a device again is what un-dissociates it. A member who re-pairs a device they had
    # removed gets the row they had, key included, rather than a second one.
    device.dissociated_at = None
    device.updated_at = now

    if existing is None:
        db.add(device)
    db.flush()
    return device


def record_installed_device(
    db: DbSession,
    *,
    user_id: UUID,
    device_id: str,
    model_id: int | None = None,
    model: str | None = None,
) -> WithingsDevice:
    """Store the device an install-success notification reported.

    Pre-registers a device ahead of Getdevice, which may not list a just-installed one
    immediately. It used to carry ``advertise_key`` too — the token the Mobile SDK needed for
    background BLE sync — and that was its real justification; with that integration abandoned
    this writer only gets a device row in slightly sooner than the next sweep would.
    """
    connection = _connection(db, user_id)
    device = _upsert(
        db,
        connection_id=connection.id,
        device_id=device_id,
        source=SOURCE_NOTIFICATION,
        model_id=model_id,
        model=model,
    )
    db.commit()

    log_structured(
        logger,
        "info",
        "Withings device recorded from the install notification",
        provider=ProviderName.WITHINGS.value,
        task="record_installed_device",
        user_id=str(user_id),
        device_id=device_id,
    )
    return device


def sync_devices_from_withings(
    db: DbSession,
    *,
    user_id: UUID,
    oauth: BaseOAuthTemplate,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> list[WithingsDevice]:
    """Reconcile the member's devices against ``User v2 - Getdevice``.

    The second source, and the only one that survives an app reinstall. Also what reconciles
    the list after the member has been inside Withings' settings WebView, where they can
    dissociate a device without our ever hearing about it.

    Devices Withings no longer lists are marked dissociated rather than deleted — see the
    model. A response that transiently omits a device would otherwise destroy that row's
    history: when it last synced, and which order it shipped on.
    """
    connection = _connection(db, user_id)

    body = withings_request(
        db=db,
        user_id=user_id,
        connection_repo=UserConnectionRepository(),
        oauth=oauth,
        service_path="/v2/user",
        action="getdevice",
        params={},
        api_base_url=api_base_url,
    )
    entries: list[WithingsDeviceEntry] = WithingsGetdeviceBody.model_validate(body).devices

    seen: set[str] = set()
    devices: list[WithingsDevice] = []
    for entry in entries:
        seen.add(entry.deviceid)
        devices.append(
            _upsert(
                db,
                connection_id=connection.id,
                device_id=entry.deviceid,
                source=SOURCE_GETDEVICE,
                model_id=entry.model_id,
                model=entry.model,
                device_type=entry.type,
                battery=entry.battery,
                last_session_at=_from_unix(entry.last_session_date),
            )
        )

    now = datetime.now(timezone.utc)
    stale = db.query(WithingsDevice).filter(
        WithingsDevice.user_connection_id == connection.id,
        WithingsDevice.dissociated_at.is_(None),
        # ONLY devices Getdevice has listed before are candidates. A device it has never
        # listed says nothing by being absent — it may simply be newer than Getdevice's view,
        # which is exactly the case ``record_installed_device`` exists for and which its own
        # docstring describes. Without this, a member pairs a scale, the app syncs before
        # Withings catches up, and the device they just paired is marked dissociated and
        # drops out of the hub until some later sync happens to rescue it.
        WithingsDevice.last_getdevice_at.isnot(None),
    )
    if seen:
        stale = stale.filter(WithingsDevice.device_id.notin_(seen))
    # An EMPTY response still marks previously-listed devices dissociated, deliberately.
    # "Withings lists no devices for this member" is a real answer and the only one they give
    # for a member who removed their last device — and the marker is soft, so a later sync
    # that lists them again simply clears it, with no row lost in between.
    missing = stale.all()
    for device in missing:
        device.dissociated_at = now
        device.updated_at = now

    db.commit()

    log_structured(
        logger,
        "info",
        "Withings devices synced from getdevice",
        provider=ProviderName.WITHINGS.value,
        task="sync_devices_from_withings",
        user_id=str(user_id),
        listed=len(devices),
        newly_dissociated=len(missing),
        without_battery=sum(1 for d in devices if not d.battery),
    )
    return devices


def mark_dissociated(db: DbSession, *, user_id: UUID, device_id: str) -> WithingsDevice | None:
    """Record that a device was removed, from the SDK's dissociation-success notification.

    Returns ``None`` when we hold no such device, which is not an error: the member may have
    dissociated one that was set up before we started recording them, or on another phone.
    """
    connection = _connection(db, user_id)
    device = (
        db.query(WithingsDevice)
        .filter(
            WithingsDevice.user_connection_id == connection.id,
            WithingsDevice.device_id == device_id,
        )
        .one_or_none()
    )
    if device is None:
        return None

    now = datetime.now(timezone.utc)
    device.dissociated_at = now
    device.updated_at = now
    db.commit()

    log_structured(
        logger,
        "info",
        "Withings device dissociated",
        provider=ProviderName.WITHINGS.value,
        task="mark_dissociated",
        user_id=str(user_id),
        device_id=device_id,
    )
    return device


def list_devices(db: DbSession, *, user_id: UUID, include_dissociated: bool = False) -> list[WithingsDevice]:
    """The member's devices, newest first, dissociated ones excluded by default."""
    connection = _connection(db, user_id)
    query = db.query(WithingsDevice).filter(WithingsDevice.user_connection_id == connection.id)
    if not include_dissociated:
        query = query.filter(WithingsDevice.dissociated_at.is_(None))
    return query.order_by(WithingsDevice.created_at.desc()).all()
