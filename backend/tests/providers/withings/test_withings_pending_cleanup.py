"""Unanswered pending weigh-ins expire after 7 days (spec 2026-10-01 D4; ruling R1: retired as tombstones)."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from celery.schedules import crontab
from sqlalchemy.orm import Session

from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.withings_pending_task import retire_stale_pending_readings
from app.models import UserConnection, WithingsMeasureGroupRecord
from app.services.providers.withings.attribution import (
    RETIRE_STALE_PENDING_TASK,
    confirm_reading,
    discard_reading,
    retire_stale_pending,
)
from app.services.providers.withings.readings import list_device_readings
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import (
    BODY_GRPID,
    HASH,
    PULSE_GRPID,
    body_group,
    days_ago,
    pulse_group,
    raw_is_null,
    records,
    samples_of,
    save,
)

_NOW = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
_TASK_MODULE = "app.integrations.celery.tasks.withings_pending_task"


def _row(db: Session, connection: UserConnection, grpid: str, at: datetime, *, status: str = "pending") -> None:
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=connection.user_id,
            user_connection_id=connection.id,
            grpid=grpid,
            device_id="dev-1",
            model="Body Pro 2",
            attrib=1,
            measured_at=at,
            status=status,
            raw={"grpid": int(grpid), "date": int(at.timestamp()), "measures": []} if status == "pending" else None,
        )
    )
    db.flush()


def test_only_pending_weigh_ins_more_than_seven_days_old_are_retired(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    _, connection = make_provisioned_connection()
    _row(db, connection, "1", _NOW - timedelta(days=7, seconds=1))
    _row(db, connection, "2", _NOW - timedelta(days=7))  # exactly 7 days: not MORE than 7
    _row(db, connection, "3", _NOW - timedelta(days=1))
    assert retire_stale_pending(db, now=_NOW) == 1
    stored = records(db, connection.id)
    assert {g: r.status for g, r in stored.items()} == {"1": "discarded", "2": "pending", "3": "pending"}
    assert raw_is_null(db, connection.id) == 1


def test_registered_and_discarded_weigh_ins_are_untouched(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    _, connection = make_provisioned_connection()
    _row(db, connection, "1", _NOW - timedelta(days=30), status="registered")
    _row(db, connection, "2", _NOW - timedelta(days=30), status="discarded")
    assert retire_stale_pending(db, now=_NOW) == 0
    assert {g: r.status for g, r in records(db, connection.id).items()} == {"1": "registered", "2": "discarded"}


def test_retiring_is_idempotent(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    _, connection = make_provisioned_connection()
    _row(db, connection, "1", _NOW - timedelta(days=8))
    assert retire_stale_pending(db, now=_NOW) == 1
    assert retire_stale_pending(db, now=_NOW) == 0


def test_a_retired_weigh_in_answers_none_to_confirm(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    at = days_ago(8)
    save(db, user.id, connection.id, [body_group(at), pulse_group(at)])
    assert retire_stale_pending(db) == 2  # both groups of the weigh-in, together
    assert confirm_reading(db, user_id=user.id, grpid=str(BODY_GRPID)) is None
    assert samples_of(db, user.id) == set()


def test_a_retired_weigh_in_is_not_resurrected_by_a_reread(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    at = days_ago(8)
    rows = [body_group(at), pulse_group(at)]
    save(db, user.id, connection.id, rows)
    retire_stale_pending(db)
    save(db, user.id, connection.id, rows)  # a reconnect backfill reads 30 days
    assert samples_of(db, user.id) == set()
    assert list_device_readings(db, user_id=user.id, device_id=HASH).items == []
    assert {r.status for r in records(db, connection.id).values()} == {"discarded"}


def test_a_retired_weigh_in_can_still_be_discarded_with_its_full_answer(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Retirement keeps the rows (only PENDING ones are touched, and only their raw is erased).

    So robin-backend's discard retry still gets grpids and measured_at back.
    """
    user, connection = make_provisioned_connection()
    at = days_ago(8)
    save(db, user.id, connection.id, [body_group(at), pulse_group(at)])
    retire_stale_pending(db)
    assert len(records(db, connection.id)) == 2  # tombstones, not deletions
    result = discard_reading(db, user_id=user.id, grpid=str(BODY_GRPID))
    assert result is not None
    assert result.was == "discarded"
    assert result.grpids == [str(BODY_GRPID), str(PULSE_GRPID)]
    assert result.measured_at == datetime.fromtimestamp(at, tz=timezone.utc)


def test_the_task_retires_in_its_own_session(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    _, connection = make_provisioned_connection()
    _row(db, connection, "1", datetime.now(timezone.utc) - timedelta(days=8))
    with patch(f"{_TASK_MODULE}.SessionLocal", return_value=nullcontext(db)):
        assert retire_stale_pending_readings() == {"retired": 1}


def test_the_task_runs_daily_from_beat() -> None:
    app = create_celery()
    entry = app.conf.beat_schedule["retire-stale-pending-withings-readings"]
    assert entry["task"] == RETIRE_STALE_PENDING_TASK
    assert entry["schedule"] == crontab(hour=3, minute=30)
    assert RETIRE_STALE_PENDING_TASK in app.tasks
    assert retire_stale_pending_readings.name == RETIRE_STALE_PENDING_TASK
