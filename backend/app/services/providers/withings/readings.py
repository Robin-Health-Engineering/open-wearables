"""Readings from the device we sold a member: one reading per weigh-in (session).

A session is the measurement groups on one connection, from one device, at one ``measured_at``
(``measure_groups.session_device``): a cellular Body Pro 2 weigh-in arrives as a body-composition
group plus a heart-pulse-only group, and the member made ONE measurement. A reading is named by
its session's representative grpid (the group holding the weight, else the lowest grpid) and
carries the union of its groups' metrics.

Only groups on a connection WE provisioned are visible here. A member's own Withings account may
hold the same kind of device, but it is not the one this feature is about, and its readings stay
in the ordinary timeseries API.

A session is ``registered``, ``pending`` (an ambiguous weigh-in awaiting the member's answer, spec
2026-10-01 D1) or ``discarded``. Discarded sessions are never served. A pending one has no samples:
its metrics come from the payload held for it (``withings_measure_group.raw``), through the same
normalisation confirming it will write samples with.
"""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_

from app.config import settings
from app.database import DbSession
from app.models import DataPointSeries, DataSource, WithingsMeasureGroupRecord
from app.schemas.enums import ProviderName, get_series_type_id
from app.services.providers.withings.connections import all_device_connection_ids, device_connections
from app.services.providers.withings.data_247 import raw_group_samples
from app.services.providers.withings.measure_groups import (
    C2_KEYS,
    DISCARDED,
    PENDING,
    REGISTERED,
    WEIGHT_KEY,
    ReadingStatus,
    grpid_order,
    representative_grpid,
    session_device,
    session_device_column,
    session_status,
)

_MAX_LIMIT = 100
_TYPE_IDS = {get_series_type_id(series): key for series, key in C2_KEYS.items()}
# data_point_series.value is numeric(10, 3); Postgres rounds half away from zero, as ROUND_HALF_UP does.
_STORED_PRECISION = Decimal("0.001")

# A cursor is ``<body>.<signature>``: the body is the last session's (measured_at, lowest grpid), the
# signature an HMAC over the body AND the member and device it was issued for. It only ever
# narrows a query that is already scoped to the requesting member's provisioned connections, so a
# forged cursor could not reach another member's rows anyway; the signature makes a tampered or
# transplanted one a clean 4xx instead of a silently different page.
_MAX_CURSOR_LENGTH = 256
_MAX_GRPID_LENGTH = 32  # WithingsMeasureGroupRecord.grpid is str_32
_SIGNATURE_BYTES = 16
_CURSOR_KEY = hmac.new(settings.secret_key.encode(), b"withings-reading-cursor", hashlib.sha256).digest()


class InvalidReadingCursor(ValueError):  # noqa: N818 -- the plan's interface name; Task 10 imports it
    """The cursor was not one this API issued for this member and device."""


@dataclass
class Reading:
    grpid: str
    measured_at: datetime
    device_id: str | None
    metrics: dict[str, float]
    is_first: bool | None = None
    status: ReadingStatus = REGISTERED


@dataclass
class ReadingPage:
    items: list[Reading]
    next_cursor: str | None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(user_id: UUID, device_id: str, body: str) -> str:
    message = f"{user_id}|{device_id}|{body}".encode()
    return _b64encode(hmac.new(_CURSOR_KEY, message, hashlib.sha256).digest()[:_SIGNATURE_BYTES])


def _encode_cursor(user_id: UUID, device_id: str, measured_at: datetime, grpid: str) -> str:
    body = _b64encode(json.dumps([measured_at.isoformat(), grpid], separators=(",", ":")).encode())
    return f"{body}.{_signature(user_id, device_id, body)}"


def _decode_cursor(user_id: UUID, device_id: str, cursor: str) -> tuple[datetime, str]:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH or not cursor.isascii() or cursor.count(".") != 1:
        raise InvalidReadingCursor("invalid cursor")
    body, signature = cursor.split(".")
    if not hmac.compare_digest(signature, _signature(user_id, device_id, body)):
        raise InvalidReadingCursor("invalid cursor")
    try:
        measured_at_text, grpid = json.loads(_b64decode(body))
        measured_at = datetime.fromisoformat(measured_at_text)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as e:
        raise InvalidReadingCursor("invalid cursor") from e
    if not isinstance(grpid, str) or len(grpid) > _MAX_GRPID_LENGTH or measured_at.tzinfo is None:
        raise InvalidReadingCursor("invalid cursor")
    return measured_at, grpid


def _provisioned_ids(db: DbSession, user_id: UUID) -> list[UUID]:
    return [c.id for c in device_connections(db, user_id)]


def _metrics(db: DbSession, user_id: UUID, records: list[WithingsMeasureGroupRecord]) -> dict[str, dict[str, float]]:
    """C2 metrics per grpid; a key the group lacks is absent, never null.

    Samples carry no connection or group row id: they join back by ``external_id = grpid`` on the
    member's Withings data source, which both of a member's connections share. ``recorded_at`` must
    also equal the group's time (every sample of a group is stamped with the group's date), so a
    stray row with a colliding ``external_id`` cannot leak into a reading.
    """
    if not records:
        return {}
    measured_at = {r.grpid: r.measured_at for r in records}
    rows = (
        db.query(
            DataPointSeries.external_id,
            DataPointSeries.series_type_definition_id,
            DataPointSeries.value,
            DataPointSeries.recorded_at,
        )
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.WITHINGS,
            DataPointSeries.external_id.in_(list(measured_at)),
            DataPointSeries.series_type_definition_id.in_(list(_TYPE_IDS)),
            DataPointSeries.recorded_at.in_(set(measured_at.values())),
        )
        .all()
    )
    out: dict[str, dict[str, float]] = {grpid: {} for grpid in measured_at}
    for grpid, type_id, value, recorded_at in rows:
        if grpid is not None and recorded_at == measured_at[grpid]:
            out[grpid][_TYPE_IDS[type_id]] = float(value)
    return out


def _raw_metrics(records: list[WithingsMeasureGroupRecord]) -> dict[str, dict[str, float]]:
    """C2 metrics of pending groups, read from their held payload: they have no samples yet.

    Built by ``raw_group_samples`` (what confirm writes) and rounded the way the samples column
    stores values, so the member sees the same numbers before and after confirming.
    """
    out: dict[str, dict[str, float]] = {}
    for record in records:
        if record.raw is None:
            continue
        metrics: dict[str, float] = {}
        for sample in raw_group_samples(
            record.raw, user_id=record.user_id, user_connection_id=record.user_connection_id
        ):
            key = C2_KEYS.get(sample.series_type)
            if key is not None:
                stored = Decimal(str(sample.value)).quantize(_STORED_PRECISION, rounding=ROUND_HALF_UP)
                metrics[key] = float(stored)
        out[record.grpid] = metrics
    return out


def session_metrics(
    db: DbSession, user_id: UUID, records: list[WithingsMeasureGroupRecord]
) -> dict[str, dict[str, float]]:
    """C2 metrics per grpid: pending groups from their held payload, every other group from its samples."""
    metrics = _metrics(db, user_id, [r for r in records if r.status != PENDING])
    metrics.update(_raw_metrics([r for r in records if r.status == PENDING]))
    return metrics


def merge_session(records: list[WithingsMeasureGroupRecord], metrics: dict[str, dict[str, float]]) -> Reading:
    """One session's groups as one reading, named by its representative grpid.

    The metrics are the union of the groups'; on a key two groups both hold (storage keeps one
    sample per series type and time, so in practice none), the representative's value wins.
    """
    rep_grpid = representative_grpid({r.grpid: WEIGHT_KEY in metrics.get(r.grpid, {}) for r in records})
    rep = next(r for r in records if r.grpid == rep_grpid)
    merged = dict(metrics.get(rep.grpid, {}))
    for record in sorted(records, key=lambda r: grpid_order(r.grpid)):
        for key, value in metrics.get(record.grpid, {}).items():
            merged.setdefault(key, value)
    return Reading(
        grpid=rep.grpid,
        measured_at=rep.measured_at,
        device_id=rep.device_id,
        metrics=merged,
        status=session_status(r.status for r in records),
    )


def _session_groups(
    db: DbSession, record: WithingsMeasureGroupRecord, *, lock: bool = False
) -> list[WithingsMeasureGroupRecord]:
    """``record`` and its siblings (same connection, same device, same ``measured_at``), in any status."""
    group = WithingsMeasureGroupRecord
    query = db.query(group).filter(
        group.user_connection_id == record.user_connection_id,
        group.measured_at == record.measured_at,
    )
    device = session_device(record.hash_device_id, record.device_id)
    # Nothing names the device, so nothing can be a sibling.
    query = query.filter(group.id == record.id) if device is None else query.filter(session_device_column() == device)
    if lock:
        query = query.with_for_update().populate_existing()
    return query.all()


def find_session(
    db: DbSession, *, user_id: UUID, grpid: str, lock: bool = False, any_state: bool = False
) -> list[WithingsMeasureGroupRecord] | None:
    """Every group of the session ``grpid`` belongs to, discarded ones included.

    Returns None unless the group is on one of this member's provisioned connections. Any grpid of
    the session names it. ``lock`` takes a row lock on the session's groups (``SELECT … FOR UPDATE``)
    so a confirm and a discard of one weigh-in, or a retried call, run one after the other.

    Only active provisioned connections count, unless ``any_state``: then a revoked or inactive one
    does too, so a repeated discard still finds the tombstone of a disconnected account.
    """
    connection_ids = all_device_connection_ids(db, user_id) if any_state else _provisioned_ids(db, user_id)
    if not connection_ids:
        return None
    group = WithingsMeasureGroupRecord
    record = (
        db.query(group)
        .filter(group.user_connection_id.in_(connection_ids), group.grpid == grpid)
        .order_by(group.measured_at.asc())
        .first()
    )
    if record is None:
        return None
    return _session_groups(db, record, lock=lock)


def list_device_readings(
    db: DbSession, *, user_id: UUID, device_id: str, limit: int = 20, cursor: str | None = None
) -> ReadingPage:
    """One device's readings on the member's provisioned connections, newest first, one per session.

    ``device_id`` matches a group's own ``deviceid`` or its ``hash_deviceid``.

    ``limit`` is clamped to 1..100 and counts sessions, not groups.
    Discarded sessions are skipped (and are not counted by ``limit``); pending ones carry their held values.
    Raises ``InvalidReadingCursor``
    for a cursor this function did not issue for this ``user_id`` and ``device_id``.
    """
    position = _decode_cursor(user_id, device_id, cursor) if cursor is not None else None
    connection_ids = _provisioned_ids(db, user_id)
    if not connection_ids:
        return ReadingPage(items=[], next_cursor=None)
    limit = max(1, min(limit, _MAX_LIMIT))
    group = WithingsMeasureGroupRecord
    device = session_device_column()
    # The session's lowest grpid (as text) breaks a tie between sessions at the same time.
    tie = func.min(group.grpid)
    query = (
        db.query(group.user_connection_id, device, group.measured_at, tie)
        .filter(
            group.user_connection_id.in_(connection_ids),
            group.status != DISCARDED,
            # Either id names the device: the device hub asks by withings_device.device_id, which for
            # the cellular Body Pro 2 is the hash, while that scale's groups carry an unrelated
            # integer deviceid beside it.
            or_(group.device_id == device_id, group.hash_device_id == device_id),
        )
        .group_by(group.user_connection_id, device, group.measured_at)
    )
    if position is not None:
        at, grpid = position
        query = query.having(or_(group.measured_at < at, and_(group.measured_at == at, tie < grpid)))
    sessions = query.order_by(group.measured_at.desc(), tie.desc()).limit(limit + 1).all()
    page, more = sessions[:limit], len(sessions) > limit
    if not page:
        return ReadingPage(items=[], next_cursor=None)

    records = (
        db.query(group)
        .filter(
            group.user_connection_id.in_({s[0] for s in page}),
            group.status != DISCARDED,
            group.measured_at.in_({s[2] for s in page}),
            device.in_({s[1] for s in page}),
        )
        .all()
    )
    by_session: dict[tuple[UUID, str | None, datetime], list[WithingsMeasureGroupRecord]] = {}
    for record in records:
        key = (record.user_connection_id, session_device(record.hash_device_id, record.device_id), record.measured_at)
        by_session.setdefault(key, []).append(record)
    metrics = session_metrics(db, user_id, records)
    items = [merge_session(by_session[(conn, dev, at)], metrics) for conn, dev, at, _ in page]
    last = page[-1]
    return ReadingPage(
        items=items,
        next_cursor=_encode_cursor(user_id, device_id, last[2], last[3]) if more else None,
    )


def get_reading(db: DbSession, *, user_id: UUID, grpid: str) -> Reading | None:
    """One reading with ``is_first`` set, or None unless the group is on a provisioned connection of this member.

    Any grpid of a session returns that whole session, under its representative grpid: an event
    or a link naming a sibling group still resolves to the reading the member saw.

    ``is_first`` means no older group exists for the same device on the same connection: a
    Withings deviceid is not unique across accounts. None as well for a discarded session (contract A1:
    detail → 404).
    """
    connection_ids = _provisioned_ids(db, user_id)
    if not connection_ids:
        return None
    group = WithingsMeasureGroupRecord
    record = (
        db.query(group)
        .filter(group.user_connection_id.in_(connection_ids), group.grpid == grpid, group.status != DISCARDED)
        .order_by(group.measured_at.asc())
        .first()
    )
    if record is None:
        return None
    siblings = [r for r in _session_groups(db, record) if r.status != DISCARDED]
    device = session_device(record.hash_device_id, record.device_id)
    if device is None:
        same_device = group.device_id.is_(None)
    else:
        same_device = session_device_column() == device
        if record.hash_device_id and record.device_id:
            # A group recorded before hash_device_id existed has only its deviceid.
            same_device = or_(same_device, and_(group.hash_device_id.is_(None), group.device_id == record.device_id))
    older = (
        db.query(group.id)
        .filter(
            group.user_connection_id == record.user_connection_id,
            same_device,
            group.measured_at < record.measured_at,
            # The member's first reading: a weigh-in they discarded, or have not confirmed, is not it.
            group.status == REGISTERED,
        )
        .first()
    )
    reading = merge_session(siblings, session_metrics(db, user_id, siblings))
    reading.is_first = older is None
    return reading
