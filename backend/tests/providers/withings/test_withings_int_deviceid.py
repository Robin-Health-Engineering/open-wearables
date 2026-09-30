"""A measure group whose ``deviceid`` is an INTEGER, as the cellular Body Pro 2 sends it.

Observed on staging 2026-09-30 against real Withings data: ``getmeas`` returns groups with
``"deviceid": 15542329`` (a JSON number) beside ``"hash_deviceid": "41a451ad…"``, while
``getdevice`` for the same account lists that device as ``deviceid = hash_deviceid = "41a451ad…"``
and ``"model": null``. The schema typed ``deviceid`` as ``str``, so Pydantic rejected every group
from the scale and nothing was ingested. And the group's integer id is not the Getdevice id at all:
only its ``hash_deviceid`` joins it to the device row (and to Robin's order).

Real session fixture: every claim here is about the rows left behind.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource, UserConnection, WithingsMeasureGroupRecord
from app.models.withings_device import WithingsDevice
from app.schemas.providers.withings import WithingsActivity, WithingsMeasureGroup, WithingsWorkout
from app.services.providers.withings import reading_events
from app.services.providers.withings._client import PaginatedResult
from app.services.providers.withings.data_247 import Withings247Data
from app.services.providers.withings.measure_groups import ParsedGroup, parsed_group_of
from app.services.providers.withings.readings import get_reading, list_device_readings
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_HASH = "41a451ad428083cbf215257be7decbc02a3169c5"
_INT_DEVICEID = 15542329
_SEND = "app.services.providers.withings.reading_events.celery_app.send_task"
_SYNC = "app.services.providers.withings.reading_events.sync_devices_from_withings"
_PAGINATE = "app.services.providers.withings.data_247.paginate"


def _body_pro_2_group(*, date: int = 1790754643, grpid: int = 8530283247, **overrides: Any) -> dict[str, Any]:
    """The shape getmeas returned for the Body Pro 2 (types as observed; values synthetic)."""
    group: dict[str, Any] = {
        "grpid": grpid,
        "attrib": 0,
        "date": date,
        "created": date + 48,
        "modified": date + 48,
        "category": 1,
        "deviceid": _INT_DEVICEID,
        "hash_deviceid": _HASH,
        "measures": [
            {"value": 72450, "type": 1, "unit": -3},  # weight
            {"value": 52100, "type": 5, "unit": -3},  # fat-free mass
            {"value": 20350, "type": 8, "unit": -3},  # fat mass
            {"value": 49400, "type": 76, "unit": -3},  # muscle mass
            {"value": 37800, "type": 77, "unit": -3},  # hydration
            {"value": 2700, "type": 88, "unit": -3},  # bone mass
            {"value": 70, "type": 170, "unit": -1},  # visceral fat
            {"value": 1650, "type": 226, "unit": 0},  # BMR
            {"value": 38, "type": 227, "unit": 0},  # metabolic age
            {"value": 28090, "type": 6, "unit": -3},  # fat ratio
        ],
        "modelid": 17,
        "model": "Body Pro 2",
        "comment": None,
        "timezone": "Europe/Rome",
    }
    group.update(overrides)
    return group


def _enable_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", "https://robin.example/reading-event")
    monkeypatch.setattr(settings, "robin_reading_event_secret", SecretStr("s3cret"))


def _getdevice_row(db: Session, connection_id: UUID, *, model: str | None = None) -> WithingsDevice:
    """The row Getdevice leaves for this scale: deviceid = hash_deviceid, no model."""
    device = WithingsDevice(
        id=uuid4(),
        user_connection_id=connection_id,
        device_id=_HASH,
        hash_device_id=_HASH,
        model_id=17,
        model=model,
        device_type="Scale",
        last_getdevice_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(device)
    db.flush()
    return device


def _save(db: Session, user_id: UUID, connection: UserConnection, rows: list[dict[str, Any]]) -> Any:
    data = Withings247Data(provider_name="withings", api_base_url="https://wbsapi.withings.net", oauth=MagicMock())
    now = datetime.now(timezone.utc)
    with patch(_PAGINATE, return_value=PaginatedResult(rows=rows, envelope={})):
        return data.save_measures(db, user_id, now - timedelta(days=1), now, connection.id)


def _recent() -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp())


# --------------------------------------------------------------------------- parsing


def test_an_integer_deviceid_parses_and_is_normalised_to_a_string() -> None:
    group = WithingsMeasureGroup.model_validate(_body_pro_2_group())
    assert group.deviceid == "15542329"
    assert group.hash_deviceid == _HASH
    assert group.model == "Body Pro 2"


def test_a_string_deviceid_still_parses_unchanged() -> None:
    group = WithingsMeasureGroup.model_validate(_body_pro_2_group(deviceid="abc123"))
    assert group.deviceid == "abc123"


def test_a_missing_or_null_deviceid_and_hash_stay_none() -> None:
    raw = _body_pro_2_group(deviceid=None)
    del raw["hash_deviceid"]
    group = WithingsMeasureGroup.model_validate(raw)
    assert group.deviceid is None
    assert group.hash_deviceid is None


def test_a_boolean_deviceid_is_still_rejected() -> None:
    # bool is an int subclass; coercing it to "True" would invent a device id.
    with pytest.raises(ValidationError, match="deviceid"):
        WithingsMeasureGroup.model_validate(_body_pro_2_group(deviceid=True))


def test_activity_and_workout_accept_an_integer_deviceid_too() -> None:
    activity = WithingsActivity.model_validate({"date": "2026-09-30", "deviceid": _INT_DEVICEID})
    workout = WithingsWorkout.model_validate({"category": 1, "startdate": 1, "enddate": 2, "deviceid": _INT_DEVICEID})
    assert activity.deviceid == "15542329"
    assert workout.deviceid == "15542329"


def test_parsed_group_carries_the_groups_own_hash() -> None:
    parsed = parsed_group_of(WithingsMeasureGroup.model_validate(_body_pro_2_group()))
    assert parsed is not None
    assert parsed.device_id == "15542329"
    assert parsed.hash_device_id == _HASH


def test_parse_measure_groups_keeps_every_body_pro_2_group() -> None:
    data = Withings247Data(provider_name="withings", api_base_url="https://wbsapi.withings.net", oauth=MagicMock())
    rows = [_body_pro_2_group(grpid=1), _body_pro_2_group(grpid=2, date=1790755700)]
    assert [g.grpid for g in data._parse_measure_groups(rows, uuid4())] == [1, 2]


# --------------------------------------------------------------------------- save_measures (the regression)


def test_save_measures_ingests_body_pro_2_groups_and_emits_with_the_groups_hash(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    _getdevice_row(db, connection.id)
    measured = _recent()

    with patch(_SEND) as send, patch(_SYNC) as sync:
        counts = _save(db, user.id, connection, [_body_pro_2_group(date=measured)])

    assert counts.inserted > 0
    measured_at = datetime.fromtimestamp(measured, tz=timezone.utc)
    weights = (
        db.query(DataPointSeries.value)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user.id, DataPointSeries.recorded_at == measured_at)
        .all()
    )
    assert Decimal("72.450") in {row.value for row in weights}

    record = db.query(WithingsMeasureGroupRecord).filter_by(user_connection_id=connection.id).one()
    assert record.grpid == "8530283247"
    assert record.device_id == "15542329"
    assert record.hash_device_id == _HASH

    sync.assert_not_called()  # the group carries its hash: no Getdevice call
    payload = send.call_args.kwargs["args"][0]
    assert set(payload) == {
        "event",
        "external_user_id",
        "withings_user_id",
        "device_id",
        "hash_deviceid",
        "grpid",
        "measured_at",
        "types",
    }
    assert payload["device_id"] == "15542329"
    assert payload["hash_deviceid"] == _HASH
    assert payload["grpid"] == "8530283247"


# --------------------------------------------------------------------------- reading events


def _parsed(grpid: str, *, device_id: str | None = "15542329", hash_device_id: str | None = _HASH) -> ParsedGroup:
    return ParsedGroup(
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=0,
        measured_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        metric_keys=("weight",),
        hash_device_id=hash_device_id,
    )


def test_a_group_with_its_own_hash_needs_no_device_row_and_no_refresh(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_events(monkeypatch)
    _, connection = make_provisioned_connection()  # no withings_device row at all
    with patch(_SEND) as send, patch(_SYNC) as sync:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_parsed("1")], oauth=MagicMock()
        )
    assert n == 1
    sync.assert_not_called()
    assert send.call_args.kwargs["args"][0]["hash_deviceid"] == _HASH


def test_a_group_without_a_hash_still_falls_back_to_the_device_row(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_events(monkeypatch)
    _, connection = make_provisioned_connection()
    db.add(
        WithingsDevice(
            id=uuid4(),
            user_connection_id=connection.id,
            device_id="dev-legacy",
            hash_device_id="hash-legacy",
            updated_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    groups = [_parsed("1"), _parsed("2", device_id="dev-legacy", hash_device_id=None)]
    with patch(_SEND) as send, patch(_SYNC) as sync:
        reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=groups, oauth=MagicMock()
        )
    sync.assert_not_called()  # the only hashless group's device already has a stored hash
    assert [c.kwargs["args"][0]["hash_deviceid"] for c in send.call_args_list] == [_HASH, "hash-legacy"]


def test_only_a_hashless_group_can_trigger_the_getdevice_refresh(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_events(monkeypatch)
    _, connection = make_provisioned_connection()
    groups = [_parsed("1"), _parsed("2", device_id="dev-unknown", hash_device_id=None)]
    with patch(_SEND) as send, patch(_SYNC) as sync:
        reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=groups, oauth=MagicMock()
        )
    sync.assert_called_once()
    assert [c.kwargs["args"][0]["hash_deviceid"] for c in send.call_args_list] == [_HASH, None]


# --------------------------------------------------------------------------- readings API


def test_readings_are_listed_under_the_getdevice_id_and_the_groups_own_id(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device hub asks by withings_device.device_id (the hash for this scale)."""
    _enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    _getdevice_row(db, connection.id)
    with patch(_SEND), patch(_SYNC):
        _save(db, user.id, connection, [_body_pro_2_group(date=_recent())])

    by_hash = list_device_readings(db, user_id=user.id, device_id=_HASH)
    by_int = list_device_readings(db, user_id=user.id, device_id="15542329")
    assert [r.grpid for r in by_hash.items] == ["8530283247"]
    assert [r.grpid for r in by_int.items] == ["8530283247"]
    assert by_hash.items[0].metrics["weight"] == pytest.approx(72.45)
    reading = get_reading(db, user_id=user.id, grpid="8530283247")
    assert reading is not None
    assert reading.is_first is True
