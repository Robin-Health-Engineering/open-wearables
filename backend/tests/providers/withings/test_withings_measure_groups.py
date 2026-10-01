"""withings_measure_group: one row per Withings measurement group per connection."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import null
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import UserConnection, WithingsMeasureGroupRecord
from app.schemas.enums import SeriesType
from app.schemas.providers.withings import WithingsMeasureGroup
from app.services.providers.withings.measure_groups import (
    C2_KEYS,
    ParsedGroup,
    parsed_group_of,
    record_new_groups,
    session_status,
)
from tests.factories import UserConnectionFactory, UserFactory


def _row(connection: UserConnection, grpid: str = "123", device_id: str | None = "dev-1") -> WithingsMeasureGroupRecord:
    return WithingsMeasureGroupRecord(
        id=uuid4(),
        user_id=connection.user_id,
        user_connection_id=connection.id,
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=0,
        measured_at=datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc),
    )


def test_a_group_is_unique_per_connection(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    db.add(_row(connection))
    db.flush()
    db.add(_row(connection))
    with pytest.raises(IntegrityError):
        db.flush()


def _parsed(grpid: str = "900", device_id: str | None = "dev-1") -> ParsedGroup:
    return ParsedGroup(
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=0,
        measured_at=datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc),
        metric_keys=("weight",),
    )


def test_parsed_group_of_keeps_device_and_mapped_keys() -> None:
    group = WithingsMeasureGroup.model_validate(
        {
            "date": 1790665200,
            "grpid": 555,
            "deviceid": "abc123",
            "model": "Body Pro 2",
            "attrib": 0,
            "measures": [
                {"value": 7000, "type": 1, "unit": -2},
                {"value": 284, "type": 6, "unit": -1},
                {"value": 5, "type": 170, "unit": 0},  # visceral fat
            ],
        }
    )
    parsed = parsed_group_of(group)
    assert parsed is not None
    assert parsed.grpid == "555"
    assert parsed.device_id == "abc123"
    assert parsed.metric_keys == ("weight", "fat_ratio", "visceral_fat")
    assert parsed.has_c2_metrics
    assert parsed.measured_at == datetime.fromtimestamp(1790665200, tz=timezone.utc)


def test_parsed_group_of_rejects_a_group_without_grpid_or_mapped_measures() -> None:
    no_grpid = WithingsMeasureGroup.model_validate({"date": 1, "measures": [{"value": 1, "type": 1, "unit": 0}]})
    # type 130 is the AFib class: deferred, so nothing maps
    unmapped = WithingsMeasureGroup.model_validate(
        {"date": 1, "grpid": 1, "measures": [{"value": 1, "type": 130, "unit": 0}]}
    )
    assert parsed_group_of(no_grpid) is None
    assert parsed_group_of(unmapped) is None


def test_group_with_only_non_c2_measures_is_kept_but_flagged() -> None:
    # Blood pressure maps to a SeriesType but is not a C2 key: recorded, yet nothing to emit.
    group = WithingsMeasureGroup.model_validate(
        {"date": 1790665200, "grpid": 7, "measures": [{"value": 120, "type": 10, "unit": 0}]}
    )
    parsed = parsed_group_of(group)
    assert parsed is not None
    assert parsed.metric_keys == ()
    assert not parsed.has_c2_metrics


def test_c2_keys_cover_exactly_the_producible_metrics() -> None:
    assert set(C2_KEYS.values()) == {
        "weight",
        "fat_ratio",
        "fat_mass",
        "muscle_mass",
        "hydration",
        "bone_mass",
        "heart_rate",
        "pulse_wave_velocity",
        "vascular_age",
        "visceral_fat",
        "basal_metabolic_rate",
    }
    assert C2_KEYS[SeriesType.cardiovascular_age] == "vascular_age"


def test_record_new_groups_returns_only_inserted(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    first = record_new_groups(
        db, user_id=connection.user_id, user_connection_id=connection.id, groups=[_parsed("1"), _parsed("2")]
    )
    assert [g.grpid for g in first] == ["1", "2"]


def test_second_insert_of_the_same_group_returns_nothing(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    kwargs = {"user_id": connection.user_id, "user_connection_id": connection.id}
    record_new_groups(db, groups=[_parsed("1")], **kwargs)
    again = record_new_groups(db, groups=[_parsed("1"), _parsed("3")], **kwargs)
    assert [g.grpid for g in again] == ["3"]


def test_same_grpid_on_another_connection_is_new(db: Session) -> None:
    user = UserFactory()
    a = UserConnectionFactory(user=user, provider="withings")
    b = UserConnectionFactory(user=user, provider="withings")
    record_new_groups(db, user_id=user.id, user_connection_id=a.id, groups=[_parsed("1")])
    again = record_new_groups(db, user_id=user.id, user_connection_id=b.id, groups=[_parsed("1")])
    assert [g.grpid for g in again] == ["1"]


def test_a_group_without_c2_metrics_is_still_recorded(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    kwargs = {"user_id": connection.user_id, "user_connection_id": connection.id}
    bp_only = ParsedGroup("8", "dev-1", None, 0, datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc), ())
    first = record_new_groups(db, groups=[bp_only], **kwargs)
    assert [g.grpid for g in first] == ["8"]
    assert not first[0].has_c2_metrics
    assert record_new_groups(db, groups=[bp_only], **kwargs) == []


# --- status and raw (spec 2026-10-01 D1) ----------------------------------------------------------


def test_a_new_group_is_registered_and_holds_no_raw(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    row = _row(connection)
    db.add(row)
    db.flush()
    db.refresh(row)
    assert row.status == "registered"
    record = WithingsMeasureGroupRecord
    assert db.query(record).filter(record.id == row.id, record.raw.is_(None)).count() == 1


def test_an_unknown_status_is_refused(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    row = _row(connection)
    row.status = "maybe"
    db.add(row)
    with pytest.raises(IntegrityError):
        db.flush()


def test_raw_round_trips_and_clearing_it_stores_sql_null(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    payload = {"grpid": 123, "attrib": 1, "measures": [{"value": 72450, "type": 1, "unit": -3}]}
    row = _row(connection)
    row.status = "pending"
    row.raw = payload
    db.add(row)
    db.flush()
    db.expire(row)
    assert row.raw == payload
    row.raw = None
    db.flush()
    record = WithingsMeasureGroupRecord
    assert db.query(record).filter(record.id == row.id, record.raw.is_(None)).count() == 1
    assert db.query(record).filter(record.id == row.id, record.raw == null()).count() == 1


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["registered"], "registered"),
        (["pending", "pending"], "pending"),
        (["discarded", "discarded"], "discarded"),
        (["discarded", "pending"], "pending"),
        (["pending", "registered"], "registered"),
        ([None], "registered"),  # a row not yet flushed carries the column default
    ],
)
def test_session_status_is_the_most_alive_one(statuses: list[str | None], expected: str) -> None:
    assert session_status(statuses) == expected
