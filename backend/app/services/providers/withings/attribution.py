"""Purchased-scale attribution: the member confirms or discards a weigh-in (spec 2026-10-01 D1-D5, D9).

A weigh-in Withings could not attribute to one user (``attrib 1``) on an account we provisioned is
held ``pending`` at ingest: its groups are recorded with their payload as received (``raw``) and no
samples. The member answers in the app ("È tua questa pesata?"), or removes a registered reading
("Non sono io"):

* **confirm** writes the samples from ``raw`` through ``raw_group_samples``, the normalisation
  ingest uses, so they are exactly what ingest would have written, and marks the session
  ``registered`` (``raw`` cleared);
* **discard** marks the session ``discarded`` (``raw`` cleared). For a registered session it also
  deletes its samples. The rows stay as tombstones: ``save_measures`` writes no samples for a
  discarded session, so no re-read of the window brings the reading back.

Both act on the whole session (``readings.find_session``: any sibling grpid names it) and are
idempotent. Neither sends an event: the member is already in the app. Both, and the 7-day
retirement, first take the session's lock (``measure_groups.lock_sessions``), the one an ingest
holds from recording a weigh-in's groups to its commit: a late sibling being ingested cannot
interleave with the member's answer (a discard could otherwise see the session without that
sibling, or the ingest write samples for a session discarded under it).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, null, select, update

from app.database import DbSession
from app.models import DataPointSeries, DataSource, WithingsMeasureGroupRecord
from app.schemas.enums import ProviderName
from app.services.providers.withings.data_247 import raw_group_samples
from app.services.providers.withings.measure_groups import (
    DISCARDED,
    PENDING,
    REGISTERED,
    ReadingStatus,
    grpid_order,
    lock_sessions,
    session_device,
    session_device_column,
    session_lock_key,
    session_status,
)
from app.services.providers.withings.readings import Reading, find_session, get_reading, merge_session, session_metrics
from app.services.timeseries_service import timeseries_service
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


def _lock_key(record: WithingsMeasureGroupRecord) -> str:
    return session_lock_key(
        record.user_connection_id,
        grpid=record.grpid,
        hash_device_id=record.hash_device_id,
        device_id=record.device_id,
        measured_at=record.measured_at,
    )


def _locked_session(
    db: DbSession, *, user_id: UUID, grpid: str, any_state: bool = False
) -> list[WithingsMeasureGroupRecord] | None:
    """The session ``grpid`` names, read again under its session lock and with its rows locked.

    Found once to learn its key, then re-read once the lock is held: an ingest that held it may
    have added a sibling meanwhile.
    """
    found = find_session(db, user_id=user_id, grpid=grpid, any_state=any_state)
    if found is None:
        return None
    lock_sessions(db, [_lock_key(found[0])])
    session = find_session(db, user_id=user_id, grpid=grpid, lock=True, any_state=any_state)
    if session is None:
        db.rollback()  # releases the lock
    return session


@dataclass(frozen=True)
class DiscardResult:
    """What a discard removed (contract A2): Robin deletes its own rows for the weigh-in by ``measured_at``."""

    grpid: str
    grpids: list[str]
    measured_at: datetime
    was: ReadingStatus


def confirm_reading(db: DbSession, *, user_id: UUID, grpid: str) -> Reading | None:
    """Register a pending weigh-in and return it as the detail route serves it.

    None when no group of it is on this member's provisioned accounts, or it was discarded or
    retired. An already registered reading is returned unchanged (idempotent).
    """
    session = _locked_session(db, user_id=user_id, grpid=grpid)
    if session is None:
        return None
    if session_status(r.status for r in session) == DISCARDED:
        db.rollback()  # releases the locks this call took
        return None
    held = [r for r in session if r.status == PENDING]
    if held:
        samples = [
            sample
            for record in held
            if record.raw is not None
            for sample in raw_group_samples(
                record.raw, user_id=record.user_id, user_connection_id=record.user_connection_id
            )
        ]
        if samples:
            timeseries_service.bulk_create_samples(db, samples)
        for record in held:
            record.status = REGISTERED
            record.raw = None
        log_structured(
            logger,
            "info",
            "Withings reading confirmed",
            provider="withings",
            action="reading_confirmed",
            user_id=str(user_id),
            grpid=grpid,
            groups=len(held),
            samples=len(samples),
        )
    db.commit()
    return get_reading(db, user_id=user_id, grpid=grpid)


def _delete_samples(db: DbSession, *, user_id: UUID, records: list[WithingsMeasureGroupRecord]) -> int:
    """Delete a session's samples: ``external_id`` among its grpids, at its time, on the member's Withings source.

    The data source is per member, not per connection (``uq_data_source_identity``: both of a
    member's Withings accounts write to the one source), so the time is what keeps a colliding grpid
    of another session out, exactly as ``readings._metrics`` joins samples to groups.
    """
    sources = select(DataSource.id).where(DataSource.user_id == user_id, DataSource.provider == ProviderName.WITHINGS)
    result = cast(
        CursorResult[tuple[()]],
        db.execute(
            delete(DataPointSeries)
            .where(
                DataPointSeries.data_source_id.in_(sources),
                DataPointSeries.external_id.in_([r.grpid for r in records]),
                DataPointSeries.recorded_at == records[0].measured_at,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount


def discard_reading(db: DbSession, *, user_id: UUID, grpid: str) -> DiscardResult | None:
    """Discard a weigh-in: the member's "No" to a pending one, or "Non sono io" on a registered one.

    Pending: the held payload is erased. Registered: its samples are deleted too. Already
    discarded: nothing changes and ``was`` says so (idempotent). None when no group of it is on this
    member's provisioned accounts.
    """
    # Any connection state: a repeat discard of a disconnected account's tombstone still answers (A2b.1).
    session = _locked_session(db, user_id=user_id, grpid=grpid, any_state=True)
    if session is None:
        return None
    was = session_status(r.status for r in session)
    # Named before anything is erased: the representative is the group holding the weight, and once
    # discarded neither samples nor raw remain to tell, so it then falls back to the lowest grpid.
    reading = merge_session(session, session_metrics(db, user_id, session))
    deleted = 0
    if was != DISCARDED:
        if any(r.status == REGISTERED for r in session):
            deleted = _delete_samples(db, user_id=user_id, records=session)
        for record in session:
            record.status = DISCARDED
            record.raw = None
        log_structured(
            logger,
            "info",
            "Withings reading discarded",
            provider="withings",
            action="reading_discarded",
            user_id=str(user_id),
            grpid=reading.grpid,
            was=was,
            samples_deleted=deleted,
        )
    db.commit()
    return DiscardResult(
        grpid=reading.grpid,
        grpids=sorted((r.grpid for r in session), key=grpid_order),
        measured_at=reading.measured_at,
        was=was,
    )


RETIRE_STALE_PENDING_TASK = "app.integrations.celery.tasks.withings_pending_task.retire_stale_pending_readings"
# Spec 2026-10-01 D4: a weigh-in nobody answered within a week is dropped, never registered.
PENDING_MAX_AGE = timedelta(days=7)


def retire_stale_pending(db: DbSession, *, now: datetime | None = None) -> int:
    """Retire every pending group measured more than 7 days ago; returns how many. Commits. Idempotent.

    "Deleted" in the spec's sense: the held values (``raw``) are erased and the weigh-in is never
    registered. The row stays as a ``discarded`` tombstone, because a re-read of the window (a
    reconnect backfill reads 30 days) would otherwise record the group again as new and pending,
    and the member would see a weigh-in they never answered come back.

    One session per transaction, under its session lock, and the whole session at once (its groups
    share ``measured_at``): a late sibling an ingest is recording right now is either committed
    before the retirement takes the lock, and retired with its session, or waits and then joins
    the session as a tombstone (R5). A session that turns stale while this runs waits for tomorrow.
    """
    cutoff = (now or datetime.now(timezone.utc)) - PENDING_MAX_AGE
    record = WithingsMeasureGroupRecord
    stale = db.execute(
        select(record.user_connection_id, record.grpid, record.hash_device_id, record.device_id, record.measured_at)
        .where(record.status == PENDING, record.measured_at < cutoff)
        .order_by(record.user_connection_id, record.measured_at)
    ).all()
    db.commit()  # ends the read's transaction: each session gets its own
    sessions: dict[str, tuple[UUID, str, str | None, datetime]] = {}
    for connection_id, grpid, hash_device_id, device_id, measured_at in stale:
        key = session_lock_key(
            connection_id, grpid=grpid, hash_device_id=hash_device_id, device_id=device_id, measured_at=measured_at
        )
        sessions.setdefault(key, (connection_id, grpid, session_device(hash_device_id, device_id), measured_at))
    retired = 0
    for key, (connection_id, grpid, device, measured_at) in sessions.items():
        lock_sessions(db, [key])
        same_session = (
            (record.grpid == grpid)
            if device is None
            else (record.measured_at == measured_at) & (session_device_column() == device)
        )
        result = cast(
            CursorResult[tuple[()]],
            db.execute(
                update(record)
                .where(record.user_connection_id == connection_id, same_session, record.status == PENDING)
                .values(status=DISCARDED, raw=null())
                .execution_options(synchronize_session=False)
            ),
        )
        db.commit()
        retired += result.rowcount
    log_structured(
        logger,
        "info",
        "Withings stale pending readings retired",
        provider="withings",
        action="pending_readings_retired",
        count=retired,
        cutoff=cutoff.isoformat(),
    )
    return retired
