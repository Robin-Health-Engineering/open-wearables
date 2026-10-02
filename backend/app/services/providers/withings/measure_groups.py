"""Group-level facts about Withings measures: which device, which account, and whether it is new.

See ``WithingsMeasureGroupRecord`` for why these live beside the samples rather than on them.
"""

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import WithingsMeasureGroupRecord
from app.models.withings_measure_group import DISCARDED, PENDING, REGISTERED, ReadingStatus
from app.schemas.enums import SeriesType
from app.schemas.providers.withings import WithingsMeasureGroup
from app.services.providers.withings.coverage import MEASURE_TYPE_MAP

# SeriesType -> the metric key Robin and the app use (spec Appendix C2). Every C2 key is producible
# since Task 3 added visceral fat (170) and BMR (226).
C2_KEYS: dict[SeriesType, str] = {
    SeriesType.weight: "weight",
    SeriesType.body_fat_percentage: "fat_ratio",
    SeriesType.body_fat_mass: "fat_mass",
    SeriesType.skeletal_muscle_mass: "muscle_mass",
    SeriesType.body_water_mass: "hydration",
    SeriesType.bone_mass: "bone_mass",
    SeriesType.heart_rate: "heart_rate",
    SeriesType.withings_pulse_wave_velocity: "pulse_wave_velocity",
    SeriesType.cardiovascular_age: "vascular_age",
    SeriesType.withings_visceral_fat: "visceral_fat",
    SeriesType.withings_basal_metabolic_rate: "basal_metabolic_rate",
}


# --- Sessions --------------------------------------------------------------------------------------
# One weigh-in can arrive as SEVERAL groups: a cellular Body Pro 2 sends the body composition in one
# grpid and the heart pulse alone in the next, both with the same ``date`` and device. A SESSION is
# the groups on one connection, from one device, at one ``measured_at``; it is one reading and one
# event. "One device" compares the hash when the group carries one, else the deviceid, so the
# integer deviceid the Body Pro 2 shares with nothing Getdevice lists never decides on its own.
WEIGHT_KEY = C2_KEYS[SeriesType.weight]


def session_device(hash_device_id: str | None, device_id: str | None) -> str | None:
    """The device half of a session key (the connection and ``measured_at`` are the rest)."""
    return hash_device_id or device_id


def session_device_column() -> ColumnElement[str | None]:
    """``session_device`` in SQL, over ``withings_measure_group``."""
    # No NULLIF for an empty hash: ``parsed_group_of`` already stores it as NULL, and a bound
    # parameter here would make the expression differ between a SELECT and its GROUP BY.
    return func.coalesce(WithingsMeasureGroupRecord.hash_device_id, WithingsMeasureGroupRecord.device_id)


def session_lock_key(
    user_connection_id: UUID,
    *,
    grpid: str,
    hash_device_id: str | None,
    device_id: str | None,
    measured_at: datetime,
) -> str:
    """The name of a session's lock: its connection, device and ``measured_at``; by grpid when nothing names a device.

    A group with neither a hash nor a deviceid has no siblings (``readings._session_groups``), so it
    is a session of its own. The prefixes keep a device string from ever spelling a grpid key.
    """
    device = session_device(hash_device_id, device_id)
    if device is None:
        return f"withings-session|{user_connection_id}|grpid|{grpid}"
    return f"withings-session|{user_connection_id}|device|{device}|{int(measured_at.timestamp())}"


def lock_sessions(db: DbSession, keys: Iterable[str]) -> None:
    """Take each session's transaction-scoped advisory lock; released by the caller's commit or rollback.

    One lock per session serialises everything that decides about a weigh-in: an ingest recording
    and announcing its groups (``Withings247Data.save_measures``), and the member's confirm or
    discard and the 7-day retirement (``attribution``). Taken in sorted order, so two callers that
    each need several sessions cannot deadlock. A hash collision between two keys only serialises
    two unrelated sessions, never lets two of one through.
    """
    for key in sorted(set(keys)):
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def grpid_order(grpid: str) -> tuple[int, int, str]:
    """Numeric order for Withings' integer grpids ("999" < "9100"); anything else after, as text."""
    return (0, int(grpid), "") if grpid.isdigit() else (1, 0, grpid)


def representative_grpid(grpids_with_weight: dict[str, bool]) -> str:
    """The grpid that stands for a session: the lowest one holding a weight, else the lowest one."""
    weighed = [g for g, has_weight in grpids_with_weight.items() if has_weight]
    return min(weighed or grpids_with_weight, key=grpid_order)


# Withings ``attrib`` 1: device-captured, but the device could not tell which of its users stepped
# on it (spec 2026-10-01 §2). On an account we provisioned such a weigh-in is held pending.
AMBIGUOUS_ATTRIB = 1


def session_status(statuses: Iterable[str | None]) -> ReadingStatus:
    """A session's status from its groups': the most alive one wins (registered > pending > discarded).

    The groups of a session share one status by construction (``record_new_groups`` decides it per
    session; confirm, discard and the retirement change all of them together), so the precedence
    only settles a mix that should not exist, and settles it safely: a session with any registered
    group still has samples for a discard to delete. ``None`` (a row never flushed) is the column
    default, registered.
    """
    seen = {status or REGISTERED for status in statuses}
    if REGISTERED in seen:
        return REGISTERED
    if PENDING in seen:
        return PENDING
    return DISCARDED


@dataclass(frozen=True)
class ParsedGroup:
    grpid: str
    device_id: str | None
    model: str | None
    attrib: int | None
    measured_at: datetime
    # C2 keys present in this group, in measure order, deduplicated. Empty when the group only
    # holds measures outside C2 (e.g. blood pressure): still recorded, but there is nothing to emit.
    metric_keys: tuple[str, ...]
    # The group's own ``hash_deviceid``. For some devices (the cellular Body Pro 2) ``device_id`` is
    # an id Getdevice never lists and only this hash joins the group to its withings_device row.
    hash_device_id: str | None = None
    # The session's status as ``record_new_groups`` decided it. Groups built anywhere else (tests,
    # ``parsed_group_of``) are registered until recorded.
    status: ReadingStatus = REGISTERED

    @property
    def has_c2_metrics(self) -> bool:
        return bool(self.metric_keys)


def parsed_group_of(group: WithingsMeasureGroup) -> ParsedGroup | None:
    """The group-level facts, or None when there is nothing to attribute.

    No grpid means no join to the samples; no mapped measure means nothing was stored.
    """
    if group.grpid is None:
        return None
    series = [MEASURE_TYPE_MAP[m.type] for m in group.measures if m.type in MEASURE_TYPE_MAP]
    if not series:
        return None
    keys = tuple(dict.fromkeys(C2_KEYS[s] for s in series if s in C2_KEYS))
    return ParsedGroup(
        grpid=str(group.grpid),
        device_id=group.deviceid,
        model=group.model,
        attrib=group.attrib,
        measured_at=datetime.fromtimestamp(group.date, tz=timezone.utc),
        metric_keys=keys,
        hash_device_id=group.hash_deviceid or None,
    )


def _stored_session_statuses(
    db: DbSession, *, user_connection_id: UUID, groups: list[ParsedGroup]
) -> dict[tuple[str, datetime], ReadingStatus]:
    """The status of every stored session these groups could join, by ``(session device, measured_at)``.

    Only sessions that name a device: a group with neither a hash nor a deviceid has no siblings
    (``readings._session_groups``), so it never inherits anything.
    """
    times = {g.measured_at for g in groups if session_device(g.hash_device_id, g.device_id) is not None}
    if not times:
        return {}
    record = WithingsMeasureGroupRecord
    rows = db.query(record.hash_device_id, record.device_id, record.measured_at, record.status).filter(
        record.user_connection_id == user_connection_id, record.measured_at.in_(times)
    )
    statuses: dict[tuple[str, datetime], list[str]] = {}
    for hash_device_id, device_id, measured_at, status in rows:
        device = session_device(hash_device_id, device_id)
        if device is not None:
            statuses.setdefault((device, measured_at), []).append(status)
    return {key: session_status(values) for key, values in statuses.items()}


def record_new_groups(
    db: DbSession,
    *,
    user_id: UUID,
    user_connection_id: UUID,
    groups: list[ParsedGroup],
    raw_by_grpid: Mapping[str, dict[str, Any]] | None = None,
    hold_ambiguous: bool = False,
) -> list[ParsedGroup]:
    """Insert the groups not yet recorded for this connection and return exactly those, with their status.

    Idempotent on ``(user_connection_id, grpid)``: a re-read window, a redelivered notification
    or a sibling appli notification for the same weigh-in returns nothing the second time. Groups
    without C2 metrics are recorded too (callers skip them via ``has_c2_metrics``). Does not
    commit; the caller commits alongside the samples.

    The status is decided per SESSION (spec 2026-10-01 D9), so a weigh-in's groups share it:

    * a session already stored (a sibling recorded by an earlier ingest) passes its status on: a
      pulse group arriving after its pending or discarded weigh-in joins it instead of registering
      on its own;
    * otherwise, with ``hold_ambiguous`` (the caller's "this is an account we provisioned"), a
      session holding an ``attrib 1`` group is ``pending``, and each of its groups keeps its
      payload from ``raw_by_grpid``, which must then hold it (``ValueError`` otherwise: a pending
      group without its payload could never be confirmed);
    * otherwise ``registered``.

    The caller holds the batch's session locks (``lock_sessions``): two ingests that each carry one
    group of a weigh-in would otherwise both miss the other's uncommitted group here, and split the
    weigh-in into a pending and a registered half.
    """
    if not groups:
        return []
    by_grpid = {g.grpid: g for g in groups}
    stored = _stored_session_statuses(db, user_connection_id=user_connection_id, groups=list(by_grpid.values()))
    batch: dict[tuple[str | None, datetime], list[ParsedGroup]] = {}
    for g in by_grpid.values():
        batch.setdefault((session_device(g.hash_device_id, g.device_id), g.measured_at), []).append(g)
    status_by_grpid: dict[str, ReadingStatus] = {}
    for (device, measured_at), members in batch.items():
        status = stored.get((device, measured_at)) if device is not None else None
        if status is None:
            ambiguous = hold_ambiguous and any(m.attrib == AMBIGUOUS_ATTRIB for m in members)
            status = PENDING if ambiguous else REGISTERED
        for member in members:
            status_by_grpid[member.grpid] = status
    raws = raw_by_grpid or {}
    unheld = sorted(grpid for grpid, status in status_by_grpid.items() if status == PENDING and grpid not in raws)
    if unheld:
        raise ValueError(f"pending groups need their raw payload: {unheld}")
    stmt = (
        insert(WithingsMeasureGroupRecord)
        .values(
            [
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "user_connection_id": user_connection_id,
                    "grpid": g.grpid,
                    "device_id": g.device_id,
                    "hash_device_id": g.hash_device_id,
                    "model": g.model,
                    "attrib": g.attrib,
                    "measured_at": g.measured_at,
                    "status": status_by_grpid[g.grpid],
                    "raw": raws[g.grpid] if status_by_grpid[g.grpid] == PENDING else None,
                }
                for g in by_grpid.values()
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_connection_id", "grpid"])
        .returning(WithingsMeasureGroupRecord.grpid)
    )
    inserted = {row[0] for row in db.execute(stmt)}
    return [dataclasses.replace(g, status=status_by_grpid[g.grpid]) for g in by_grpid.values() if g.grpid in inserted]


def withheld_grpids(db: DbSession, *, user_connection_id: UUID, grpids: Iterable[str]) -> set[str]:
    """Which of these grpids belong to a pending or discarded session: their samples must not be written.

    Asked on every ingest, after ``record_new_groups``, for the new groups AND every re-read of an
    old one, so a discarded reading's samples never come back on the next sync of its window, and a
    pending one's are only ever written by confirm.
    """
    wanted = set(grpids)
    if not wanted:
        return set()
    record = WithingsMeasureGroupRecord
    rows = db.query(record.grpid).filter(
        record.user_connection_id == user_connection_id, record.grpid.in_(wanted), record.status != REGISTERED
    )
    return {row[0] for row in rows}
