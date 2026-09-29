"""Readings from the device we sold a member: one per Withings measurement group."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, SeriesTypeDefinition, User, UserConnection, WithingsMeasureGroupRecord
from app.schemas.enums import ProviderName, SeriesType, get_series_type_id
from app.services.providers.withings.readings import (
    InvalidReadingCursor,
    get_reading,
    list_device_readings,
)
from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserConnectionFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)

_SERIES = {
    "weight": SeriesType.weight,
    "fat_ratio": SeriesType.body_fat_percentage,
    "heart_rate": SeriesType.heart_rate,
    "vascular_age": SeriesType.cardiovascular_age,
    "visceral_fat": SeriesType.withings_visceral_fat,
    "basal_metabolic_rate": SeriesType.withings_basal_metabolic_rate,
}

# One Withings data source per member, as in production: both of a member's connections write to
# it (uq_data_source_identity). Safe across tests: every test makes fresh users and rolls back.
_SOURCES: dict[UUID, DataSource] = {}


def _source(user: User) -> DataSource:
    if user.id not in _SOURCES:
        _SOURCES[user.id] = DataSourceFactory(
            user=user, provider=ProviderName.WITHINGS, device_model=None, source="withings", device_type=None
        )
    return _SOURCES[user.id]


def _reading(
    db: Session,
    user: User,
    connection: UserConnection,
    grpid: str,
    at: datetime,
    device_id: str = "dev-1",
    **metrics: float,
) -> None:
    """A group and its samples. Callers keep ``at`` distinct per member (uq_data_point_series_source_type_time)."""
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=user.id,
            user_connection_id=connection.id,
            grpid=grpid,
            device_id=device_id,
            model="Body Pro 2",
            attrib=0,
            measured_at=at,
        )
    )
    source = _source(user)
    for key, value in metrics.items():
        definition = db.get(SeriesTypeDefinition, get_series_type_id(_SERIES[key]))
        DataPointSeriesFactory(
            data_source=source, series_type=definition, value=Decimal(str(value)), recorded_at=at, external_id=grpid
        )
    db.flush()


def test_list_is_newest_first_with_metrics(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "1", _T0, weight=70.0, fat_ratio=28.4)
    _reading(db, user, connection, "2", _T0 + timedelta(days=1), weight=69.5)
    page = list_device_readings(db, user_id=user.id, device_id="dev-1")
    assert [r.grpid for r in page.items] == ["2", "1"]
    assert page.items[1].metrics == {"weight": 70.0, "fat_ratio": 28.4}
    assert page.items[0].metrics == {"weight": 69.5}  # absent keys are omitted, not null
    assert page.items[0].measured_at == _T0 + timedelta(days=1)
    assert page.items[0].device_id == "dev-1"
    assert page.next_cursor is None


def test_list_paginates_by_cursor(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    for i in range(5):
        _reading(db, user, connection, str(i), _T0 + timedelta(hours=i), weight=70 + i)
    first = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=2)
    second = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=2, cursor=first.next_cursor)
    third = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=2, cursor=second.next_cursor)
    assert [r.grpid for r in first.items + second.items + third.items] == ["4", "3", "2", "1", "0"]
    assert first.next_cursor is not None
    assert second.next_cursor is not None
    assert third.next_cursor is None


def test_list_breaks_a_time_tie_by_grpid(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    for grpid in ("a", "b", "c"):
        _reading(db, user, connection, grpid, _T0)  # a group with no stored samples still lists
    first = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=2)
    second = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=2, cursor=first.next_cursor)
    assert [r.grpid for r in first.items + second.items] == ["c", "b", "a"]
    assert second.items[0].metrics == {}


def test_a_sample_off_the_groups_time_is_not_its_metric(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Samples join by external_id = grpid; one stamped at another time is not this group's."""
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "1", _T0, weight=70)
    definition = db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.heart_rate))
    DataPointSeriesFactory(
        data_source=_source(user),
        series_type=definition,
        value=Decimal("99"),
        recorded_at=_T0 + timedelta(hours=3),
        external_id="1",
    )
    db.flush()
    reading = get_reading(db, user_id=user.id, grpid="1")
    assert reading is not None
    assert reading.metrics == {"weight": 70.0}


def test_limit_is_clamped(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    for i in range(3):
        _reading(db, user, connection, str(i), _T0 + timedelta(hours=i))
    assert len(list_device_readings(db, user_id=user.id, device_id="dev-1", limit=0).items) == 1
    assert len(list_device_readings(db, user_id=user.id, device_id="dev-1", limit=10_000).items) == 3


def test_list_excludes_other_devices_and_self_linked_accounts(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    own = UserConnectionFactory(user=user, provider="withings")  # member's own account, no sdk row
    _reading(db, user, connection, "mine", _T0, weight=70)
    _reading(db, user, connection, "other-device", _T0 + timedelta(minutes=1), device_id="dev-2", weight=71)
    _reading(db, user, own, "own-account", _T0 + timedelta(minutes=2), weight=72)
    assert [r.grpid for r in list_device_readings(db, user_id=user.id, device_id="dev-1").items] == ["mine"]


def test_list_of_another_members_device_is_empty(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    alice, alice_conn = make_provisioned_connection()
    bob, _ = make_provisioned_connection()
    _reading(db, alice, alice_conn, "a1", _T0, weight=70)
    page = list_device_readings(db, user_id=bob.id, device_id="dev-1")
    assert page.items == []
    assert page.next_cursor is None


def test_bad_cursor_raises(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, _ = make_provisioned_connection()
    for cursor in ("not-a-cursor", "", "a.b", "x" * 10_000):
        with pytest.raises(InvalidReadingCursor):
            list_device_readings(db, user_id=user.id, device_id="dev-1", cursor=cursor)


def test_a_tampered_cursor_raises(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    for i in range(3):
        _reading(db, user, connection, str(i), _T0 + timedelta(hours=i))
    cursor = list_device_readings(db, user_id=user.id, device_id="dev-1", limit=1).next_cursor
    assert cursor is not None
    body, signature = cursor.split(".")
    flipped = body[:-1] + ("A" if body[-1] != "A" else "B")
    with pytest.raises(InvalidReadingCursor):
        list_device_readings(db, user_id=user.id, device_id="dev-1", cursor=f"{flipped}.{signature}")


def test_a_cursor_is_bound_to_its_member_and_device(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    alice, alice_conn = make_provisioned_connection()
    bob, bob_conn = make_provisioned_connection()
    for i in range(2):
        _reading(db, alice, alice_conn, f"a{i}", _T0 + timedelta(hours=i))
        _reading(db, alice, alice_conn, f"d{i}", _T0 + timedelta(hours=i, minutes=30), device_id="dev-2")
        _reading(db, bob, bob_conn, f"b{i}", _T0 + timedelta(hours=i))
    cursor = list_device_readings(db, user_id=alice.id, device_id="dev-1", limit=1).next_cursor
    with pytest.raises(InvalidReadingCursor):
        list_device_readings(db, user_id=bob.id, device_id="dev-1", cursor=cursor)
    with pytest.raises(InvalidReadingCursor):
        list_device_readings(db, user_id=alice.id, device_id="dev-2", cursor=cursor)


def test_get_reading_sets_is_first(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    metrics = {"weight": 70, "heart_rate": 68, "vascular_age": 42, "visceral_fat": 8, "basal_metabolic_rate": 1620}
    _reading(db, user, connection, "1", _T0, **metrics)
    _reading(db, user, connection, "2", _T0 + timedelta(days=1), weight=69)
    first = get_reading(db, user_id=user.id, grpid="1")
    later = get_reading(db, user_id=user.id, grpid="2")
    assert first is not None
    assert first.is_first is True
    assert first.metrics == {
        "weight": 70.0,
        "heart_rate": 68.0,
        "vascular_age": 42.0,
        "visceral_fat": 8.0,
        "basal_metabolic_rate": 1620.0,
    }
    assert later is not None
    assert later.is_first is False
    assert later.metrics == {"weight": 69.0}


def test_is_first_ignores_the_same_deviceid_on_other_accounts(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """A Withings deviceid is not unique across accounts: only this connection's device history counts."""
    user, connection = make_provisioned_connection()
    own = UserConnectionFactory(user=user, provider="withings")
    _, historic = make_provisioned_connection(user=user)  # a second provisioned account (historic members)
    other, other_conn = make_provisioned_connection()
    _reading(db, user, own, "own-old", _T0 - timedelta(days=30), weight=80)
    _reading(db, user, historic, "historic-old", _T0 - timedelta(days=20), weight=85)
    _reading(db, other, other_conn, "other-old", _T0 - timedelta(days=30), weight=90)
    _reading(db, user, connection, "older-other-device", _T0 - timedelta(days=10), device_id="dev-2", weight=75)
    _reading(db, user, connection, "first", _T0, weight=70)
    reading = get_reading(db, user_id=user.id, grpid="first")
    assert reading is not None
    assert reading.is_first is True


def test_reading_on_self_linked_account_is_none(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, _ = make_provisioned_connection()
    own = UserConnectionFactory(user=user, provider="withings")
    _reading(db, user, own, "own", _T0, weight=70)
    assert get_reading(db, user_id=user.id, grpid="own") is None


def test_reading_of_another_member_is_none(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    alice, alice_conn = make_provisioned_connection()
    bob, _ = make_provisioned_connection()
    _reading(db, alice, alice_conn, "a1", _T0, weight=70)
    assert get_reading(db, user_id=bob.id, grpid="a1") is None
    assert get_reading(db, user_id=alice.id, grpid="a1") is not None
