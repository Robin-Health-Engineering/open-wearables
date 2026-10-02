"""The readings API sees attribution: status per reading, pending values from the held payload, discarded gone.

Contract A1; spec 2026-10-01 §4.1 "Readings API"; ruling R6 (is_first counts registered sessions only).
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import User, UserConnection, WithingsMeasureGroupRecord
from app.services.providers.withings.readings import get_reading, list_device_readings
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import BODY_GRPID, HASH, PULSE_GRPID, body_group, pulse_group

_T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)


def _row(
    db: Session,
    user: User,
    connection: UserConnection,
    grpid: int,
    at: datetime,
    *,
    status: str,
    held: dict[str, Any] | None = None,
) -> None:
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=user.id,
            user_connection_id=connection.id,
            grpid=str(grpid),
            device_id="15542329",
            hash_device_id=HASH,
            model="Body Pro 2",
            attrib=1 if status == "pending" else 0,
            measured_at=at,
            status=status,
            raw=held,
        )
    )
    db.flush()


def _pending_weigh_in(db: Session, user: User, connection: UserConnection, at: datetime = _T0) -> None:
    date = int(at.timestamp())
    _row(db, user, connection, BODY_GRPID, at, status="pending", held=body_group(date))
    _row(db, user, connection, PULSE_GRPID, at, status="pending", held=pulse_group(date))


def test_a_pending_weigh_in_lists_with_its_metrics_from_the_held_groups(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _pending_weigh_in(db, user, connection)
    (item,) = list_device_readings(db, user_id=user.id, device_id=HASH).items
    assert item.grpid == str(BODY_GRPID)
    assert item.status == "pending"
    assert item.metrics == {
        "weight": 72.45,
        "fat_ratio": 28.09,
        "fat_mass": 20.35,
        "bone_mass": 2.735,
        "heart_rate": 64.0,
    }


def test_pending_metrics_round_like_stored_samples(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """data_point_series.value is numeric(10, 3): 2.7345 is stored as 2.735, so it is shown as 2.735 too."""
    user, connection = make_provisioned_connection()
    _pending_weigh_in(db, user, connection)
    reading = get_reading(db, user_id=user.id, grpid=str(BODY_GRPID))
    assert reading is not None
    assert reading.metrics["bone_mass"] == 2.735


def test_detail_of_a_pending_sibling_is_the_whole_pending_weigh_in(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _pending_weigh_in(db, user, connection)
    reading = get_reading(db, user_id=user.id, grpid=str(PULSE_GRPID))
    assert reading is not None
    assert reading.grpid == str(BODY_GRPID)
    assert reading.status == "pending"
    assert reading.metrics["heart_rate"] == 64.0
    assert reading.metrics["weight"] == 72.45
    assert reading.is_first is True


def test_a_registered_reading_says_so(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    _row(db, user, connection, 1, _T0, status="registered")
    (item,) = list_device_readings(db, user_id=user.id, device_id=HASH).items
    assert item.status == "registered"
    reading = get_reading(db, user_id=user.id, grpid="1")
    assert reading is not None
    assert reading.status == "registered"


def test_a_discarded_weigh_in_is_neither_listed_nor_served(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _row(db, user, connection, 1, _T0, status="discarded")
    _row(db, user, connection, 2, _T0 + timedelta(hours=1), status="registered")
    assert [r.grpid for r in list_device_readings(db, user_id=user.id, device_id=HASH).items] == ["2"]
    assert get_reading(db, user_id=user.id, grpid="1") is None


def test_discarded_weigh_ins_do_not_count_toward_a_page(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _row(db, user, connection, 1, _T0, status="registered")
    _row(db, user, connection, 2, _T0 + timedelta(hours=1), status="discarded")
    _row(db, user, connection, 3, _T0 + timedelta(hours=2), status="registered")
    first = list_device_readings(db, user_id=user.id, device_id=HASH, limit=1)
    second = list_device_readings(db, user_id=user.id, device_id=HASH, limit=1, cursor=first.next_cursor)
    assert [r.grpid for r in first.items] == ["3"]
    assert [r.grpid for r in second.items] == ["1"]
    assert second.next_cursor is None


def test_is_first_counts_only_registered_older_weigh_ins(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    older_pending = _T0 - timedelta(days=1)
    _row(db, user, connection, 10, _T0 - timedelta(days=2), status="discarded")
    _row(
        db,
        user,
        connection,
        11,
        older_pending,
        status="pending",
        held=body_group(int(older_pending.timestamp()), grpid=11),
    )
    _row(db, user, connection, 12, _T0, status="registered")
    reading = get_reading(db, user_id=user.id, grpid="12")
    assert reading is not None
    assert reading.is_first is True

    _row(db, user, connection, 9, _T0 - timedelta(days=3), status="registered")
    reading = get_reading(db, user_id=user.id, grpid="12")
    assert reading is not None
    assert reading.is_first is False
