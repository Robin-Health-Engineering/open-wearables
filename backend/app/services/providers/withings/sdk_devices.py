"""The two writers of ``advertise_key``, and the reconciliation between them.

Background BLE sync is what makes a Withings device useful between Wi-Fi sessions, and the
SDK cannot start it without a per-device ``advertise_key``. Withings hands that token out two
ways and states plainly that **both must be implemented**:

* the SDK's **install-success notification**, which only the app ever sees, and
* **``User v2 - Getdevice``**, a token-authenticated call only the server can make.

Neither is complete on its own. The notification is the only source for a device installed
while Getdevice had not caught up, and Getdevice is the only source after an app reinstall,
which loses every notification the app ever received. So the rule throughout this module is:
**a write never erases a key it cannot replace.** A Getdevice entry with no ``advertise_key``
leaves the stored one alone; it does not null it.

Reference: https://developer.withings.com/sdk/v2/tree/sdk-webviews/required-web-services/
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

# Recorded on the row so that "where did this key come from" is answerable when a device
# stops syncing. Diagnostic only — neither source outranks the other, the later write wins.
#
# A Literal rather than a bare str so every call site is checked, and NOT a SQLAlchemy Enum
# column: mapping one would mean an entry in ``BaseDbModel.type_annotation_map``, which lives
# in ``app/database.py`` — an upstream file this fork has to keep rebasing onto cleanly. The
# column stays a plain String; the constraint is enforced where the values are written.
DeviceKeySource = Literal["notification", "getdevice"]

SOURCE_NOTIFICATION: DeviceKeySource = "notification"
SOURCE_GETDEVICE: DeviceKeySource = "getdevice"


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
    source: DeviceKeySource,
    model_id: int | None = None,
    model: str | None = None,
    device_type: str | None = None,
    advertise_key: str | None = None,
    last_session_at: datetime | None = None,
) -> WithingsDevice:
    """Create or update one device row, without ever losing what the other writer stored.

    Every optional field is written only when a value is actually supplied. That is the whole
    mechanism: a Getdevice entry carrying no ``advertise_key`` must leave the one the install
    notification stored exactly where it is, because nothing else can re-derive it.
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
    if advertise_key:
        device.advertise_key = advertise_key
        device.advertise_key_source = source
    if last_session_at is not None:
        device.last_session_at = last_session_at

    # Stamped for a Getdevice write and ONLY a Getdevice write — it records that Withings'
    # own list has seen this device, which is what makes the dissociation sweep safe. It is
    # deliberately not derived from ``advertise_key_source``: a Getdevice entry carrying no
    # advertise_key leaves that field saying "notification", and a sweep keyed on it would
    # still sweep devices Getdevice knows about.
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
    advertise_key: str | None = None,
) -> WithingsDevice:
    """Store what the SDK's install-success notification reported.

    The first of the two sources, and often the only one that will ever carry this device's
    ``advertise_key`` — Getdevice may not list a just-installed device immediately, and the
    notification is not repeated.
    """
    connection = _connection(db, user_id)
    device = _upsert(
        db,
        connection_id=connection.id,
        device_id=device_id,
        source=SOURCE_NOTIFICATION,
        model_id=model_id,
        model=model,
        advertise_key=advertise_key,
    )
    db.commit()

    log_structured(
        logger,
        "info" if advertise_key else "warning",
        "Withings device recorded from the install notification",
        provider=ProviderName.WITHINGS.value,
        task="record_installed_device",
        user_id=str(user_id),
        device_id=device_id,
        # Worth a warning rather than an info: a BLE device installed without a key will not
        # sync in the background, and this is the moment that becomes true.
        has_advertise_key=bool(device.advertise_key),
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
    model. A response that transiently omits a device would otherwise destroy an
    ``advertise_key`` that only the install notification ever carried.
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
                advertise_key=entry.advertise_key,
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
    # that lists them again clears it and the advertise_keys were never at risk.
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
        without_advertise_key=sum(1 for d in devices if not d.advertise_key),
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
