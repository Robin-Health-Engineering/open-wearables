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

**Every entry point spans ALL of a member's Withings connections unless one is named.** A member
can hold several — their own linked account, plus one per cellular device we shipped them — and
each device row is keyed by the connection whose account it sits on. Resolving the primary only,
as this module did, meant a shipped device was invisible to ``list_devices`` and never reached by
the Getdevice sweep, which called as the personal account. The sweep still runs PER connection,
because a device missing from one account's response says nothing about a device on another.

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
from app.services.providers.withings.connections import active_withings_connections
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


def _connection(db: DbSession, user_id: UUID, connection_id: UUID | None = None) -> UserConnection:
    """One Withings connection of this member's: the named one, else their primary.

    A member can hold several — their own linked account, plus one per cellular device we ship
    them — so "the member's Withings connection" is no longer a complete description, and the
    callers that write a device row have to say which account it belongs to.

    The named lookup is scoped to the member for the reason ``api_client._resolve_connection``
    gives: an id fetched bare returns whatever row it names, and this one decides whose device
    table is written.
    """
    repo = UserConnectionRepository()
    if connection_id is not None:
        connection = repo.get(db, connection_id)
        if connection is None or connection.user_id != user_id or connection.provider != ProviderName.WITHINGS.value:
            raise WithingsDeviceError("this member has no such Withings connection")
        return connection
    connection = repo.get_by_user_and_provider(db, user_id, ProviderName.WITHINGS.value)
    if connection is None:
        raise WithingsDeviceError("this member has no Withings connection")
    return connection


def _connections(db: DbSession, user_id: UUID, connection_id: UUID | None = None) -> list[UserConnection]:
    """Every active Withings connection of this member's, or just the one named.

    The default is ALL of them, and that is the point. A member who linked their own account and
    was then shipped a device has devices hanging off two accounts; reading or sweeping only the
    primary leaves the shipped one invisible and never reconciled — which is precisely the member
    this whole change exists for.
    """
    if connection_id is not None:
        return [_connection(db, user_id, connection_id)]
    connections = active_withings_connections(db, user_id)
    if not connections:
        raise WithingsDeviceError("this member has no Withings connection")
    return connections


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
    connection_id: UUID | None = None,
) -> WithingsDevice:
    """Store the device an install-success notification reported.

    Pre-registers a device ahead of Getdevice, which may not list a just-installed one
    immediately. It used to carry ``advertise_key`` too — the token the Mobile SDK needed for
    background BLE sync — and that was its real justification; with that integration abandoned
    this writer only gets a device row in slightly sooner than the next sweep would.
    """
    connection = _connection(db, user_id, connection_id)
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
    connection_id: UUID | None = None,
) -> list[WithingsDevice]:
    """Reconcile the member's devices against ``User v2 - Getdevice``.

    Sweeps EVERY Withings account the member holds unless one is named. Sweeping only the
    primary would call Getdevice as the personal account, so a cellular device on an account we
    provisioned is never listed, never has its battery or last-session updated, and never
    reconciles — invisible to the very member the multi-account work is for.

    The second source, and the only one that survives an app reinstall. Also what reconciles
    the list after the member has been inside Withings' settings WebView, where they can
    dissociate a device without our ever hearing about it.

    Devices Withings no longer lists are marked dissociated rather than deleted — see the
    model. A response that transiently omits a device would otherwise destroy that row's
    history: when it last synced, and which order it shipped on.
    """
    swept: list[WithingsDevice] = []
    for connection in _connections(db, user_id, connection_id):
        swept.extend(_sync_one(db, user_id=user_id, connection=connection, oauth=oauth, api_base_url=api_base_url))
    return swept


def _sync_one(
    db: DbSession,
    *,
    user_id: UUID,
    connection: UserConnection,
    oauth: BaseOAuthTemplate,
    api_base_url: str = WITHINGS_API_BASE_URL,
) -> list[WithingsDevice]:
    """Reconcile ONE Withings account's devices.

    Per connection, not per member, and the stale sweep is why: a device absent from account A's
    Getdevice response says nothing about a device on account B, so judging them together would
    dissociate every device on the account that was not asked.
    """
    body = withings_request(
        db=db,
        user_id=user_id,
        connection_id=connection.id,
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
    # Across every one of the member's Withings accounts: the caller names a DEVICE, and which
    # of their accounts it hangs off is our bookkeeping rather than something they can know.
    connection_ids = [c.id for c in _connections(db, user_id)]
    device = (
        db.query(WithingsDevice)
        .filter(
            WithingsDevice.user_connection_id.in_(connection_ids),
            WithingsDevice.device_id == device_id,
        )
        .first()
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


def list_devices(
    db: DbSession,
    *,
    user_id: UUID,
    include_dissociated: bool = False,
    connection_id: UUID | None = None,
) -> list[WithingsDevice]:
    """The member's devices, newest first, dissociated ones excluded by default.

    Spans every Withings account the member holds unless one is named. A member sees "my
    devices", not "my devices on the account this happens to have been shipped against".
    """
    connection_ids = [c.id for c in _connections(db, user_id, connection_id)]
    query = db.query(WithingsDevice).filter(WithingsDevice.user_connection_id.in_(connection_ids))
    if not include_dissociated:
        query = query.filter(WithingsDevice.dissociated_at.is_(None))
    return query.order_by(WithingsDevice.created_at.desc()).all()
