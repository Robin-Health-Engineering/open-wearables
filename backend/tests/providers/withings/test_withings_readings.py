"""Readings from the device we sold a member: one per weigh-in (session of measurement groups)."""

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
    merge_session,
)
from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserConnectionFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)

_SERIES = {
    "weight": SeriesType.weight,
    "fat_ratio": SeriesType.body_fat_percentage,
    "fat_mass": SeriesType.body_fat_mass,
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
    device_id: str | None = "dev-1",
    hash_device_id: str | None = None,
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
            hash_device_id=hash_device_id,
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
    """Same device and time on three provisioned connections: three sessions, ordered by grpid."""
    user, connection = make_provisioned_connection()
    _, second_conn = make_provisioned_connection(user=user)
    _, third_conn = make_provisioned_connection(user=user)
    for grpid, conn in (("a", connection), ("b", second_conn), ("c", third_conn)):
        _reading(db, user, conn, grpid, _T0)  # a group with no stored samples still lists
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
    for cursor in ("not-a-cursor", "", "a.b", "a.é", "x" * 10_000):
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


def test_another_members_sample_with_the_same_grpid_and_time_is_not_a_metric(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """The sample join is scoped to the requesting member's data source, not only by grpid and time."""
    alice, alice_conn = make_provisioned_connection()
    bob, _ = make_provisioned_connection()
    _reading(db, alice, alice_conn, "1", _T0, weight=70)
    definition = db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.heart_rate))
    DataPointSeriesFactory(
        data_source=_source(bob), series_type=definition, value=Decimal("99"), recorded_at=_T0, external_id="1"
    )
    db.flush()
    reading = get_reading(db, user_id=alice.id, grpid="1")
    assert reading is not None
    assert reading.metrics == {"weight": 70.0}


# --- One weigh-in = one reading -------------------------------------------------------------------
# The real shape, from a cellular Body Pro 2 on staging (2026-10-01): one weigh-in arrives as two
# groups with the same date and device, the body composition in one and the heart pulse alone in
# the next grpid.
_HASH = "41a451ad0c5e7f2b9d3a6c8e1f4b7a2d5c8e0f3a"
_DEVICE = "15542329"
_BODY = {"weight": 72.4, "fat_ratio": 21.3, "fat_mass": 15.4}


def _weigh_in(
    db: Session,
    user: User,
    connection: UserConnection,
    at: datetime,
    body_grpid: str = "8530283247",
    pulse_grpid: str = "8530283250",
    *,
    pulse: float = 64,
    pulse_first: bool = False,
) -> None:
    def body() -> None:
        _reading(db, user, connection, body_grpid, at, _DEVICE, _HASH, **_BODY)

    def heart() -> None:
        _reading(db, user, connection, pulse_grpid, at, _DEVICE, _HASH, heart_rate=pulse)

    for add in (heart, body) if pulse_first else (body, heart):
        add()


def test_a_weigh_in_split_in_two_groups_lists_as_one_reading(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _weigh_in(db, user, connection, _T0)
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["8530283247"]
    assert page.items[0].metrics == {**_BODY, "heart_rate": 64.0}
    assert page.items[0].measured_at == _T0
    assert page.items[0].device_id == _DEVICE
    assert page.next_cursor is None
    # The device hub may also ask by the integer deviceid: same single reading.
    assert [r.grpid for r in list_device_readings(db, user_id=user.id, device_id=_DEVICE).items] == ["8530283247"]


def test_the_representative_is_the_weight_group_even_when_its_grpid_is_higher(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _weigh_in(db, user, connection, _T0, body_grpid="8530283250", pulse_grpid="8530283247", pulse_first=True)
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["8530283250"]
    assert page.items[0].metrics == {**_BODY, "heart_rate": 64.0}


def test_without_a_weight_group_the_lowest_grpid_represents_the_session(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "9100", _T0, _DEVICE, _HASH, heart_rate=60)
    _reading(db, user, connection, "999", _T0, _DEVICE, _HASH, vascular_age=40)  # numerically lower
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["999"]
    assert page.items[0].metrics == {"heart_rate": 60.0, "vascular_age": 40.0}


def test_on_a_key_in_two_groups_the_representative_wins() -> None:
    """Storage keeps one sample per (source, type, time), so samples cannot collide; the rule is pinned anyway."""
    t = _T0
    records = [
        WithingsMeasureGroupRecord(grpid="8530283246", device_id=_DEVICE, hash_device_id=_HASH, measured_at=t),
        WithingsMeasureGroupRecord(grpid="8530283247", device_id=_DEVICE, hash_device_id=_HASH, measured_at=t),
    ]
    metrics = {"8530283246": {"heart_rate": 64.0}, "8530283247": {"weight": 72.4, "heart_rate": 70.0}}
    reading = merge_session(records, metrics)
    assert reading.grpid == "8530283247"
    assert reading.metrics == {"weight": 72.4, "heart_rate": 70.0}


def test_two_weigh_ins_are_two_readings(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection = make_provisioned_connection()
    _weigh_in(db, user, connection, _T0)
    _weigh_in(db, user, connection, _T0 + timedelta(minutes=20), "8530358979", "8530358986", pulse=71)
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["8530358979", "8530283247"]
    assert page.items[0].metrics == {**_BODY, "heart_rate": 71.0}
    assert page.items[1].metrics == {**_BODY, "heart_rate": 64.0}


def test_groups_at_different_times_are_not_merged(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "8530283247", _T0, _DEVICE, _HASH, **_BODY)
    _reading(db, user, connection, "8530283250", _T0 + timedelta(seconds=1), _DEVICE, _HASH, heart_rate=64)
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["8530283250", "8530283247"]
    assert page.items[0].metrics == {"heart_rate": 64.0}


def test_groups_from_different_devices_are_not_merged(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Same time and the same integer deviceid, but different hashes: two devices, two readings."""
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "8530283247", _T0, _DEVICE, _HASH, **_BODY)
    _reading(db, user, connection, "8530283250", _T0, _DEVICE, "another-hash", heart_rate=64)
    by_int_id = list_device_readings(db, user_id=user.id, device_id=_DEVICE)
    assert [r.grpid for r in by_int_id.items] == ["8530283250", "8530283247"]
    assert [r.grpid for r in list_device_readings(db, user_id=user.id, device_id=_HASH).items] == ["8530283247"]
    detail = get_reading(db, user_id=user.id, grpid="8530283250")
    assert detail is not None
    assert detail.metrics == {"heart_rate": 64.0}


def test_hashless_groups_merge_on_their_deviceid(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "11", _T0, weight=70)
    _reading(db, user, connection, "12", _T0, heart_rate=60)
    _reading(db, user, connection, "13", _T0, device_id="dev-2", vascular_age=41)
    page = list_device_readings(db, user_id=user.id, device_id="dev-1")
    assert [(r.grpid, r.metrics) for r in page.items] == [("11", {"weight": 70.0, "heart_rate": 60.0})]


def test_the_same_weigh_in_on_two_connections_is_not_merged(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _, historic = make_provisioned_connection(user=user)
    _reading(db, user, connection, "8530283247", _T0, _DEVICE, _HASH, weight=72.4)
    _reading(db, user, historic, "8530283250", _T0, _DEVICE, _HASH)
    page = list_device_readings(db, user_id=user.id, device_id=_HASH)
    assert [r.grpid for r in page.items] == ["8530283250", "8530283247"]


def test_limit_and_cursor_count_weigh_ins_not_groups(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    for i in range(3):
        _weigh_in(db, user, connection, _T0 + timedelta(hours=i), f"85302832{i}0", f"85302832{i}5")
    first = list_device_readings(db, user_id=user.id, device_id=_HASH, limit=2)
    assert [r.grpid for r in first.items] == ["8530283220", "8530283210"]
    assert all("heart_rate" in r.metrics and "weight" in r.metrics for r in first.items)
    assert first.next_cursor is not None
    second = list_device_readings(db, user_id=user.id, device_id=_HASH, limit=2, cursor=first.next_cursor)
    assert [r.grpid for r in second.items] == ["8530283200"]
    assert second.items[0].metrics == {**_BODY, "heart_rate": 64.0}
    assert second.next_cursor is None
    assert len(list_device_readings(db, user_id=user.id, device_id=_HASH, limit=3).items) == 3


def test_detail_by_the_representative_or_its_sibling_is_the_same_merged_reading(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    _weigh_in(db, user, connection, _T0)
    by_body = get_reading(db, user_id=user.id, grpid="8530283247")
    by_pulse = get_reading(db, user_id=user.id, grpid="8530283250")
    assert by_body is not None
    assert by_body.grpid == "8530283247"
    assert by_body.metrics == {**_BODY, "heart_rate": 64.0}
    assert by_body.measured_at == _T0
    assert by_body.device_id == _DEVICE
    assert by_body.is_first is True
    assert by_pulse == by_body


def test_is_first_is_per_weigh_in(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    """The pulse group of the first weigh-in does not make that weigh-in's own body group 'not first'."""
    user, connection = make_provisioned_connection()
    _weigh_in(db, user, connection, _T0)
    _weigh_in(db, user, connection, _T0 + timedelta(days=1), "8530358979", "8530358986")
    for grpid in ("8530283247", "8530283250"):
        first = get_reading(db, user_id=user.id, grpid=grpid)
        assert first is not None
        assert first.is_first is True
    for grpid in ("8530358979", "8530358986"):
        later = get_reading(db, user_id=user.id, grpid=grpid)
        assert later is not None
        assert later.grpid == "8530358979"
        assert later.is_first is False


def test_is_first_sees_an_older_weigh_in_recorded_before_the_hash_column(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Groups recorded before migration a3c9d51e7f20 carry no hash; they still are this device's history."""
    user, connection = make_provisioned_connection()
    _reading(db, user, connection, "100", _T0 - timedelta(days=1), _DEVICE, None, weight=73)
    _weigh_in(db, user, connection, _T0)
    reading = get_reading(db, user_id=user.id, grpid="8530283250")
    assert reading is not None
    assert reading.is_first is False
