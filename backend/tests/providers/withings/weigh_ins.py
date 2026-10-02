"""A real cellular Body Pro 2 weigh-in, run through the real ``save_measures`` (only Withings is stubbed).

Shared by the attribution tests (spec 2026-10-01). The measures are the shape staging returned on
2026-10-01: the body composition in one group and the heart pulse alone in the next, same date and
device. Bone mass is given at a 10^-4 unit on purpose: ``data_point_series.value`` is numeric(10, 3),
so it is the value that shows whether a pending reading's numbers match the stored ones.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Query, Session

from app.config import settings
from app.models import DataPointSeries, DataSource, WithingsMeasureGroupRecord
from app.repositories.data_point_series_repository import WriteCounts
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings import reading_events
from app.services.providers.withings._client import PaginatedResult
from app.services.providers.withings.data_247 import Withings247Data
from app.services.providers.withings.measure_groups import ParsedGroup

HASH = "41a451ad428083cbf215257be7decbc02a3169c5"
BODY_GRPID = 8530283247
PULSE_GRPID = 8530283250
SEND = "app.services.providers.withings.reading_events.celery_app.send_task"
_PAGINATE = "app.services.providers.withings.data_247.paginate"


def body_group(date: int, *, grpid: int = BODY_GRPID, attrib: int = 1, **overrides: Any) -> dict[str, Any]:
    group: dict[str, Any] = {
        "grpid": grpid,
        "attrib": attrib,
        "date": date,
        "created": date + 48,
        "modified": date + 48,
        "category": 1,
        "deviceid": 15542329,
        "hash_deviceid": HASH,
        "measures": [
            {"value": 72450, "type": 1, "unit": -3},  # weight 72.45
            {"value": 28090, "type": 6, "unit": -3},  # fat ratio 28.09
            {"value": 20350, "type": 8, "unit": -3},  # fat mass 20.35
            {"value": 27345, "type": 88, "unit": -4},  # bone mass 2.7345, stored as 2.735
        ],
        "modelid": 17,
        "model": "Body Pro 2",
        "comment": None,
        "timezone": "Europe/Rome",
    }
    group.update(overrides)
    return group


def pulse_group(date: int, *, grpid: int = PULSE_GRPID, attrib: int = 1, **overrides: Any) -> dict[str, Any]:
    return body_group(date, grpid=grpid, attrib=attrib, measures=[{"value": 64, "type": 11, "unit": 0}], **overrides)


def recent() -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp())


def days_ago(days: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())


def enable_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", "https://robin.example/reading-event")
    monkeypatch.setattr(settings, "robin_reading_event_secret", SecretStr("s3cret"))


def save(
    db: Session,
    user_id: UUID,
    connection_id: UUID,
    rows: list[dict[str, Any]],
    *,
    envelope: dict[str, Any] | None = None,
) -> WriteCounts:
    data = Withings247Data(provider_name="withings", api_base_url="https://wbsapi.withings.net", oauth=MagicMock())
    now = datetime.now(timezone.utc)
    with patch(_PAGINATE, return_value=PaginatedResult(rows=rows, envelope=envelope or {})):
        return data.save_measures(db, user_id, now - timedelta(days=1), now, connection_id)


def announce(
    db: Session,
    *,
    user_connection_id: UUID,
    groups: list[ParsedGroup],
    now: datetime | None = None,
    oauth: BaseOAuthTemplate | None = None,
) -> int:
    """The reading-event half of an ingest, as ``save_measures`` runs it: decide, then send (no commit between)."""
    plan = reading_events.decide_reading_events(db, user_connection_id=user_connection_id, groups=groups, now=now)
    return reading_events.send_reading_events(db, plan, oauth=oauth)


def _member_samples(db: Session, user_id: UUID) -> Query[DataPointSeries]:
    return (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id)
    )


def samples_of(db: Session, user_id: UUID) -> set[tuple[Any, ...]]:
    """Every sample of the member, as (type, value, time, external_id, zone_offset)."""
    return {
        (s.series_type_definition_id, s.value, s.recorded_at, s.external_id, s.zone_offset)
        for s in _member_samples(db, user_id)
    }


def samples_at(db: Session, user_id: UUID, date: int) -> list[DataPointSeries]:
    at = datetime.fromtimestamp(date, tz=timezone.utc)
    return _member_samples(db, user_id).filter(DataPointSeries.recorded_at == at).all()


def records(db: Session, connection_id: UUID) -> dict[str, WithingsMeasureGroupRecord]:
    rows = db.query(WithingsMeasureGroupRecord).filter_by(user_connection_id=connection_id).all()
    for row in rows:
        db.refresh(row)
    return {row.grpid: row for row in rows}


def raw_is_null(db: Session, connection_id: UUID) -> int:
    """How many of the connection's groups hold SQL NULL in ``raw``."""
    record = WithingsMeasureGroupRecord
    return db.query(record).filter(record.user_connection_id == connection_id, record.raw.is_(None)).count()
