"""Confirm / discard routes and the readings API's status, at the HTTP boundary (contracts A1, A2)."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeries, SeriesTypeDefinition, User, UserConnection, WithingsMeasureGroupRecord
from app.schemas.enums import ProviderName, SeriesType, get_series_type_id
from tests.factories import DataPointSeriesFactory, DataSourceFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
_BASE = "/api/v1/providers/withings/sdk"
_WEIGHT = [{"value": 72450, "type": 1, "unit": -3}]
_PULSE = [{"value": 64, "type": 11, "unit": 0}]
_NOT_FOUND = {"detail": "reading_not_found"}


def _held(grpid: int, measures: list[dict[str, int]]) -> dict[str, Any]:
    return {
        "grpid": grpid,
        "attrib": 1,
        "date": int(_T0.timestamp()),
        "deviceid": "dev-1",
        "timezone": "Europe/Rome",
        "measures": measures,
    }


def _add(
    db: Session,
    user: User,
    connection: UserConnection,
    grpid: str,
    *,
    status: str = "registered",
    raw: dict[str, Any] | None = None,
) -> None:
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=user.id,
            user_connection_id=connection.id,
            grpid=grpid,
            device_id="dev-1",
            model="Body Pro 2",
            attrib=1 if status == "pending" else 0,
            measured_at=_T0,
            status=status,
            raw=raw,
        )
    )
    db.commit()


def _pending_weigh_in(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> User:
    user, connection = make_provisioned_connection()
    _add(db, user, connection, "55", status="pending", raw=_held(55, _WEIGHT))
    _add(db, user, connection, "56", status="pending", raw=_held(56, _PULSE))
    return user


def test_a_pending_weigh_in_lists_as_pending_with_its_values(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _pending_weigh_in(db, make_provisioned_connection)
    r = client.get(f"{_BASE}/devices/dev-1/readings", params={"user_id": str(user.id)}, headers=api_key_header)
    assert r.status_code == 200
    assert r.json()["items"] == [
        {
            "grpid": "55",
            "measured_at": "2026-09-01T07:00:00Z",
            "device_id": "dev-1",
            "metrics": {"weight": 72.45, "heart_rate": 64.0},
            "status": "pending",
        }
    ]


def test_confirm_answers_the_registered_reading_and_is_idempotent(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}
    expected = {
        "grpid": "55",
        "measured_at": "2026-09-01T07:00:00Z",
        "device_id": "dev-1",
        "metrics": {"weight": 72.45, "heart_rate": 64.0},
        "status": "registered",
        "is_first": True,
    }

    first = client.post(f"{_BASE}/readings/56/confirm", params=params, headers=api_key_header)
    again = client.post(f"{_BASE}/readings/55/confirm", params=params, headers=api_key_header)
    detail = client.get(f"{_BASE}/readings/55", params=params, headers=api_key_header)

    assert first.status_code == again.status_code == detail.status_code == 200
    assert first.json() == again.json() == detail.json() == expected


def test_confirm_of_a_missing_or_discarded_reading_is_404(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}
    missing = client.post(f"{_BASE}/readings/999/confirm", params=params, headers=api_key_header)
    assert (missing.status_code, missing.json()) == (404, _NOT_FOUND)
    client.post(f"{_BASE}/readings/55/discard", params=params, headers=api_key_header)
    discarded = client.post(f"{_BASE}/readings/55/confirm", params=params, headers=api_key_header)
    assert (discarded.status_code, discarded.json()) == (404, _NOT_FOUND)


def test_discard_of_a_pending_weigh_in_answers_the_contract_and_is_idempotent(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}

    first = client.post(f"{_BASE}/readings/56/discard", params=params, headers=api_key_header)
    again = client.post(f"{_BASE}/readings/55/discard", params=params, headers=api_key_header)

    body = {"grpid": "55", "grpids": ["55", "56"], "measured_at": "2026-09-01T07:00:00Z"}
    assert first.status_code == again.status_code == 200
    assert first.json() == {**body, "was": "pending"}
    assert again.json() == {**body, "was": "discarded"}
    assert client.get(f"{_BASE}/readings/55", params=params, headers=api_key_header).status_code == 404
    listed = client.get(f"{_BASE}/devices/dev-1/readings", params=params, headers=api_key_header)
    assert listed.json() == {"items": [], "next_cursor": None}


def test_discard_of_a_registered_reading_removes_its_samples(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    _add(db, user, connection, "55")
    source = DataSourceFactory(
        user=user, provider=ProviderName.WITHINGS, device_model=None, source="withings", device_type=None
    )
    DataPointSeriesFactory(
        data_source=source,
        series_type=db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.weight)),
        value=Decimal("72.45"),
        recorded_at=_T0,
        external_id="55",
    )
    db.commit()

    r = client.post(f"{_BASE}/readings/55/discard", params={"user_id": str(user.id)}, headers=api_key_header)

    assert r.status_code == 200
    assert r.json()["was"] == "registered"
    assert db.query(DataPointSeries).filter(DataPointSeries.external_id == "55").count() == 0


def test_discard_of_a_missing_or_foreign_reading_is_404(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    _pending_weigh_in(db, make_provisioned_connection)
    stranger, _ = make_provisioned_connection()
    params = {"user_id": str(stranger.id)}
    for grpid in ("55", "999"):
        r = client.post(f"{_BASE}/readings/{grpid}/discard", params=params, headers=api_key_header)
        assert (r.status_code, r.json()) == (404, _NOT_FOUND)


def test_rediscarding_keeps_answering_the_full_body_from_the_tombstones(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    """robin-backend retries a failed cleanup by calling discard AGAIN: it needs measured_at and grpids every time.

    So discarded groups are never hard-deleted: their rows still hold grpid and measured_at.
    """
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}
    client.post(f"{_BASE}/readings/55/discard", params=params, headers=api_key_header)
    for _ in range(2):
        r = client.post(f"{_BASE}/readings/56/discard", params=params, headers=api_key_header)
        assert r.status_code == 200
        assert r.json() == {
            "grpid": "55",
            "grpids": ["55", "56"],
            "measured_at": "2026-09-01T07:00:00Z",
            "was": "discarded",
        }
    record = WithingsMeasureGroupRecord
    assert db.query(record).filter(record.user_id == user.id, record.status == "discarded").count() == 2


def test_a_generic_404_is_not_reading_not_found(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    """Robin maps exactly {"detail": "reading_not_found"} to ReadingNotFound; any other 404 must not look like it."""
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}
    unrouted = client.post(f"{_BASE}/readings/55/not-a-route", params=params, headers=api_key_header)
    assert unrouted.status_code == 404
    assert unrouted.json() == {"detail": "Not Found"}
    assert unrouted.json() != _NOT_FOUND
    # The existing GET detail keeps its own 404 body (ruling R7).
    stranger, _ = make_provisioned_connection()
    detail = client.get(f"{_BASE}/readings/55", params={"user_id": str(stranger.id)}, headers=api_key_header)
    assert detail.status_code == 404
    assert detail.json() == {"detail": "No such reading for this member"}
    assert detail.json() != _NOT_FOUND


def test_confirm_and_discard_require_authentication(
    client: TestClient, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user = _pending_weigh_in(db, make_provisioned_connection)
    params = {"user_id": str(user.id)}
    assert client.post(f"{_BASE}/readings/55/confirm", params=params).status_code == 401
    assert client.post(f"{_BASE}/readings/55/discard", params=params).status_code == 401
