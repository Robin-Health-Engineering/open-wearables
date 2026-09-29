"""Readings from the device we sold a member: one reading per Withings measurement group.

Only groups on a connection WE provisioned are visible here. A member's own Withings account may
hold the same kind of device, but it is not the one this feature is about, and its readings stay
in the ordinary timeseries API.
"""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_

from app.config import settings
from app.database import DbSession
from app.models import DataPointSeries, DataSource, WithingsMeasureGroupRecord
from app.schemas.enums import ProviderName, get_series_type_id
from app.services.providers.withings.connections import device_connections
from app.services.providers.withings.measure_groups import C2_KEYS

_MAX_LIMIT = 100
_TYPE_IDS = {get_series_type_id(series): key for series, key in C2_KEYS.items()}

# A cursor is ``<body>.<signature>``: the body is the last item's (measured_at, grpid), the
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


def _encode_cursor(user_id: UUID, device_id: str, record: WithingsMeasureGroupRecord) -> str:
    body = _b64encode(json.dumps([record.measured_at.isoformat(), record.grpid], separators=(",", ":")).encode())
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


def list_device_readings(
    db: DbSession, *, user_id: UUID, device_id: str, limit: int = 20, cursor: str | None = None
) -> ReadingPage:
    """One device's readings on the member's provisioned connections, newest first.

    ``limit`` is clamped to 1..100. Raises ``InvalidReadingCursor`` for a cursor this function did
    not issue for this ``user_id`` and ``device_id``.
    """
    position = _decode_cursor(user_id, device_id, cursor) if cursor is not None else None
    connection_ids = _provisioned_ids(db, user_id)
    if not connection_ids:
        return ReadingPage(items=[], next_cursor=None)
    limit = max(1, min(limit, _MAX_LIMIT))
    query = db.query(WithingsMeasureGroupRecord).filter(
        WithingsMeasureGroupRecord.user_connection_id.in_(connection_ids),
        WithingsMeasureGroupRecord.device_id == device_id,
    )
    if position is not None:
        at, grpid = position
        query = query.filter(
            or_(
                WithingsMeasureGroupRecord.measured_at < at,
                and_(WithingsMeasureGroupRecord.measured_at == at, WithingsMeasureGroupRecord.grpid < grpid),
            )
        )
    records = (
        query.order_by(WithingsMeasureGroupRecord.measured_at.desc(), WithingsMeasureGroupRecord.grpid.desc())
        .limit(limit + 1)
        .all()
    )
    page, more = records[:limit], len(records) > limit
    metrics = _metrics(db, user_id, page)
    return ReadingPage(
        items=[Reading(r.grpid, r.measured_at, r.device_id, metrics[r.grpid]) for r in page],
        next_cursor=_encode_cursor(user_id, device_id, page[-1]) if more else None,
    )


def get_reading(db: DbSession, *, user_id: UUID, grpid: str) -> Reading | None:
    """One reading with ``is_first`` set, or None unless the group is on a provisioned connection of this member.

    ``is_first`` means no older group exists for the same device on the same connection: a
    Withings deviceid is not unique across accounts.
    """
    connection_ids = _provisioned_ids(db, user_id)
    if not connection_ids:
        return None
    record = (
        db.query(WithingsMeasureGroupRecord)
        .filter(
            WithingsMeasureGroupRecord.user_connection_id.in_(connection_ids),
            WithingsMeasureGroupRecord.grpid == grpid,
        )
        .order_by(WithingsMeasureGroupRecord.measured_at.asc())
        .first()
    )
    if record is None:
        return None
    older = (
        db.query(WithingsMeasureGroupRecord.id)
        .filter(
            WithingsMeasureGroupRecord.user_connection_id == record.user_connection_id,
            WithingsMeasureGroupRecord.device_id == record.device_id,
            WithingsMeasureGroupRecord.measured_at < record.measured_at,
        )
        .first()
    )
    return Reading(
        grpid=record.grpid,
        measured_at=record.measured_at,
        device_id=record.device_id,
        metrics=_metrics(db, user_id, [record])[record.grpid],
        is_first=older is None,
    )
