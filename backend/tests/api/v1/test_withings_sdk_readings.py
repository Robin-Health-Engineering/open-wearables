from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, UserConnection, WithingsMeasureGroupRecord
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
_LIST_URL = "/api/v1/providers/withings/sdk/devices/dev-1/readings"
_ONE_URL = "/api/v1/providers/withings/sdk/readings/55"


def _add_group(
    db: Session, user: User, connection: UserConnection, grpid: str = "55", device_id: str = "dev-1"
) -> None:
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=user.id,
            user_connection_id=connection.id,
            grpid=grpid,
            device_id=device_id,
            model="Body Pro 2",
            attrib=0,
            measured_at=_T0,
        )
    )
    db.commit()


def _provisioned_member(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> User:
    user, connection = make_provisioned_connection()
    _add_group(db, user, connection)
    return user


def test_list_route_shape(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _provisioned_member(db, make_provisioned_connection)

    r = client.get(_LIST_URL, params={"user_id": str(user.id)}, headers=api_key_header)

    assert r.status_code == 200
    assert r.json() == {
        "items": [{"grpid": "55", "measured_at": "2026-09-01T07:00:00Z", "device_id": "dev-1", "metrics": {}}],
        "next_cursor": None,
    }


def test_list_route_pages_with_cursor(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    _add_group(db, user, connection, grpid="55")
    _add_group(db, user, connection, grpid="56")

    first = client.get(_LIST_URL, params={"user_id": str(user.id), "limit": 1}, headers=api_key_header).json()
    second = client.get(
        _LIST_URL, params={"user_id": str(user.id), "limit": 1, "cursor": first["next_cursor"]}, headers=api_key_header
    ).json()

    assert [i["grpid"] for i in first["items"]] == ["56"]
    assert [i["grpid"] for i in second["items"]] == ["55"]
    assert second["next_cursor"] is None


def test_list_route_for_member_without_provisioned_account_is_empty(
    client: TestClient, db: Session, api_key_header: dict[str, str]
) -> None:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="withings")
    _add_group(db, user, connection)

    r = client.get(_LIST_URL, params={"user_id": str(user.id)}, headers=api_key_header)

    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


def test_single_route_shape(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _provisioned_member(db, make_provisioned_connection)

    r = client.get(_ONE_URL, params={"user_id": str(user.id)}, headers=api_key_header)

    assert r.status_code == 200
    assert r.json() == {
        "grpid": "55",
        "measured_at": "2026-09-01T07:00:00Z",
        "device_id": "dev-1",
        "is_first": True,
        "metrics": {},
    }


def test_reading_on_self_linked_account_is_404(client: TestClient, db: Session, api_key_header: dict[str, str]) -> None:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="withings")
    _add_group(db, user, connection)

    r = client.get(_ONE_URL, params={"user_id": str(user.id)}, headers=api_key_header)

    assert r.status_code == 404


def test_reading_of_another_member_is_404(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    _provisioned_member(db, make_provisioned_connection)
    stranger, _ = make_provisioned_connection()

    r = client.get(_ONE_URL, params={"user_id": str(stranger.id)}, headers=api_key_header)

    assert r.status_code == 404


def test_bad_cursor_is_400(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _provisioned_member(db, make_provisioned_connection)

    r = client.get(_LIST_URL, params={"user_id": str(user.id), "cursor": "garbage"}, headers=api_key_header)

    assert r.status_code == 400


def test_limit_out_of_range_is_rejected(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user = _provisioned_member(db, make_provisioned_connection)

    r = client.get(_LIST_URL, params={"user_id": str(user.id), "limit": 0}, headers=api_key_header)

    # This app maps request-validation errors to 400 (see the app-level handler), not FastAPI's 422.
    assert r.status_code == 400


def test_requires_authentication(
    client: TestClient, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user = _provisioned_member(db, make_provisioned_connection)

    assert client.get(_ONE_URL, params={"user_id": str(user.id)}).status_code == 401
    assert client.get(_LIST_URL, params={"user_id": str(user.id)}).status_code == 401
