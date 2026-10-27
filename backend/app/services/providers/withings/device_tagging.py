"""Tag a device as one Robin sold the member, and announce what it already measured (spec 2026-10-27 O3, contract C2).

robin-backend matches a member's Withings device to the order that shipped it (by ``hash_deviceid``)
and calls ``PUT /withings/sdk/devices/{device_id}/order-ref``. On the member's OWN Withings account
that tag is what makes the device "the scale we sold" (``connections.robin_device_scope``), so the
first tag there also re-decides the reading events of the device's recent weigh-ins
(``reading_events.decide_tagged_device``): Health Mate setup usually takes a first weigh-in before
the member links the account, and the OAuth backfill lands before the tag.

Announce-once. The tag and the re-decision run in ONE transaction holding the connection's
EXCLUSIVE tag lock (``measure_groups.lock_device_tags``); every ingest holds the same lock SHARED
while it decides. So a weigh-in is decided by exactly one of: an ingest that already saw the device
tagged; or this re-decision, after which an ingest of a re-read or a late sibling sees it as decided.
Only the FIRST tag of a device on a self-linked connection re-decides: a provisioned connection's
weigh-ins were announced at ingest, and a re-tag (a replacement order) finds them decided.

Its own module because ``reading_events`` already imports ``sdk_devices`` (the Getdevice refresh).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from app.database import DbSession
from app.models.withings_device import WithingsDevice
from app.schemas.enums import ProviderName
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings.connections import active_withings_connections, device_connections
from app.services.providers.withings.measure_groups import lock_device_tags
from app.services.providers.withings.reading_events import (
    ReadingEventPlan,
    decide_tagged_device,
    send_reading_events,
)
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


def set_order_ref(
    db: DbSession,
    *,
    user_id: UUID,
    device_id: str,
    order_ref: str,
    oauth: BaseOAuthTemplate | None = None,
    now: datetime | None = None,
) -> WithingsDevice | None:
    """Record which robin-backend order shipped this device. Idempotent; None if not the member's.

    Across ALL the member's ACTIVE Withings connections (contract C2): an account we provisioned for a
    cellular device, or the member's own account holding the scale they bought. Still only their own:
    another member's device is None and untouched. When the same ``device_id`` sits on two of the
    member's accounts, the provisioned row is tagged. A soft-dissociated device is still taggable.

    Robin re-issues this on every match, so a repeat costs a read and a lock and writes nothing. A
    DIFFERENT value overwrites: Robin is authoritative for orders. ``oauth`` lets an announced weigh-in
    whose device has no stored hash refresh Getdevice once (``reading_events.send_reading_events``).
    """
    connections = active_withings_connections(db, user_id)
    if not connections:
        return None
    provisioned = {c.id for c in device_connections(db, user_id)}
    candidates = (
        db.query(WithingsDevice)
        .filter(
            WithingsDevice.user_connection_id.in_([c.id for c in connections]),
            WithingsDevice.device_id == device_id,
        )
        .all()
    )
    if not candidates:
        return None
    device = min(candidates, key=lambda d: (d.user_connection_id not in provisioned, d.created_at))

    # BEFORE reading the current tag: a concurrent tagging of this device, or an ingest deciding on
    # this connection, finishes first, and the refresh below sees what it committed.
    lock_device_tags(db, device.user_connection_id, exclusive=True)
    db.refresh(device)
    if device.order_ref == order_ref:
        db.commit()  # nothing written; ends the transaction and releases the lock
        return device

    previous = device.order_ref
    device.order_ref = order_ref
    device.updated_at = datetime.now(timezone.utc)
    db.flush()
    plan: ReadingEventPlan | None = None
    if previous is None and device.user_connection_id not in provisioned:
        plan = decide_tagged_device(db, device=device, now=now)
    db.commit()

    if previous is not None:
        log_structured(
            logger,
            "info",
            "Withings device order_ref replaced",
            provider=ProviderName.WITHINGS.value,
            action="order_ref_replaced",
            user_id=str(user_id),
            device_id=device_id,
            previous=previous,
            current=order_ref,
        )
    # After the commit: Robin reads the reading back as soon as it gets the event. Never raises.
    send_reading_events(db, plan, oauth=oauth)
    return device
