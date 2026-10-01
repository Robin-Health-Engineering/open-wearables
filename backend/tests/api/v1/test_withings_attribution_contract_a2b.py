"""Contract A2b at the real HTTP boundaries: repeat discard of tombstones, and held / discarded samples never served.

1. A repeat discard of an already-discarded session, including one retired by the 7-day cleanup, answers 200
   (``was: discarded``). Only a session that never existed answers 404 ``reading_not_found``.
2. The timeseries API the app reads back through never serves a sample of a pending or discarded weigh-in: not
   after the ingest, not after the discard, not after a re-read or a wider backfill window.

The data comes from the real ``save_measures`` (only Withings is stubbed); every assertion goes through the
real routes with the real API-key auth.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.attribution import retire_stale_pending
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import BODY_GRPID, PULSE_GRPID, body_group, days_ago, pulse_group, recent, save

_SDK = "/api/v1/providers/withings/sdk"
_NOT_FOUND = {"detail": "reading_not_found"}
_TYPES = ["weight", "heart_rate", "body_fat_percentage", "body_fat_mass", "bone_mass"]
_BODY_TYPES = {"weight", "body_fat_percentage", "body_fat_mass", "bone_mass"}


def _iso(at: int) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _discarded_body(at: int) -> dict[str, Any]:
    return {
        "grpid": str(BODY_GRPID),
        "grpids": sorted([str(BODY_GRPID), str(PULSE_GRPID)]),
        "measured_at": _iso(at),
        "was": "discarded",
    }


def _served(client: TestClient, headers: dict[str, str], user: User) -> list[dict[str, Any]]:
    """Every sample the timeseries API serves the member, over a window wider than any weigh-in used here."""
    now = datetime.now(timezone.utc)
    r = client.get(
        f"/api/v1/users/{user.id}/timeseries",
        params={
            "start_time": (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "types": _TYPES,
            "limit": 100,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pagination"]["has_more"] is False
    return body["data"]


def _served_types(client: TestClient, headers: dict[str, str], user: User) -> set[str]:
    return {str(s["type"]) for s in _served(client, headers, user)}


def _discard(client: TestClient, headers: dict[str, str], user_id: UUID, grpid: int | str) -> Any:
    return client.post(f"{_SDK}/readings/{grpid}/discard", params={"user_id": str(user_id)}, headers=headers)


def _ambiguous(at: int) -> list[dict[str, Any]]:
    return [body_group(at), pulse_group(at)]


def _unambiguous(at: int) -> list[dict[str, Any]]:
    return [body_group(at, attrib=0), pulse_group(at, attrib=0)]


def _reread_and_backfill(db: Session, user: User, connection: UserConnection, rows: list[dict[str, Any]]) -> None:
    """The same window again, then a wider one that also holds an unrelated older weigh-in (a reconnect backfill)."""
    save(db, user.id, connection.id, rows)
    other = [body_group(days_ago(20), grpid=1, attrib=0), pulse_group(days_ago(20), grpid=2, attrib=0)]
    save(db, user.id, connection.id, other + rows)


def test_a_repeat_discard_of_a_tombstone_retired_by_the_cleanup_answers_200(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    at = days_ago(8)
    save(db, user.id, connection.id, _ambiguous(at))
    assert retire_stale_pending(db) == 2

    for _ in range(2):
        r = _discard(client, api_key_header, user.id, BODY_GRPID)
        assert r.status_code == 200
        assert r.json() == _discarded_body(at)
    # Any grpid of the session answers the same.
    other = _discard(client, api_key_header, user.id, PULSE_GRPID)
    assert (other.status_code, other.json()) == (200, _discarded_body(at))


def test_a_repeat_discard_after_the_connection_was_revoked_answers_200_and_only_a_stranger_grpid_is_404(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    at = recent()
    save(db, user.id, connection.id, _ambiguous(at))
    first = _discard(client, api_key_header, user.id, BODY_GRPID)
    assert (first.status_code, first.json()["was"]) == (200, "pending")

    connection.status = ConnectionStatus.REVOKED
    db.commit()

    again = _discard(client, api_key_header, user.id, BODY_GRPID)
    assert (again.status_code, again.json()) == (200, _discarded_body(at))
    never = _discard(client, api_key_header, user.id, 424242)
    assert (never.status_code, never.json()) == (404, _NOT_FOUND)


def test_the_timeseries_api_serves_nothing_of_a_pending_weigh_in(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    save(db, user.id, connection.id, _ambiguous(recent()))

    assert _served(client, api_key_header, user) == []


def test_the_timeseries_api_stops_serving_a_registered_weigh_in_once_it_is_discarded(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    at = recent()
    save(db, user.id, connection.id, _unambiguous(at))
    # Meaningful only if the samples really are served before the discard.
    assert _served_types(client, api_key_header, user) == _BODY_TYPES | {"heart_rate"}

    r = _discard(client, api_key_header, user.id, BODY_GRPID)
    assert (r.status_code, r.json()["was"]) == (200, "registered")

    assert _served(client, api_key_header, user) == []


def test_a_reread_or_backfill_never_brings_a_discarded_weigh_in_back_into_the_timeseries(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    rows = _unambiguous(recent())
    save(db, user.id, connection.id, rows)
    assert _discard(client, api_key_header, user.id, BODY_GRPID).status_code == 200

    _reread_and_backfill(db, user, connection, rows)

    # The unrelated 20-day-old weigh-in is registered and served; the discarded one is not.
    served = _served(client, api_key_header, user)
    assert {str(s["type"]) for s in served} == _BODY_TYPES | {"heart_rate"}
    assert {s["timestamp"][:10] for s in served} == {
        (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    }


def test_a_reread_or_backfill_never_brings_a_pending_weigh_in_into_the_timeseries(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    rows = _ambiguous(recent())
    save(db, user.id, connection.id, rows)
    assert _served(client, api_key_header, user) == []

    _reread_and_backfill(db, user, connection, rows)

    served = _served(client, api_key_header, user)
    assert {s["timestamp"][:10] for s in served} == {
        (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    }


def test_a_reread_or_backfill_never_brings_a_cleanup_retired_weigh_in_into_the_timeseries(
    client: TestClient,
    db: Session,
    api_key_header: dict[str, str],
    make_provisioned_connection: ProvisionedConnectionMaker,
) -> None:
    user, connection = make_provisioned_connection()
    at = days_ago(8)
    rows = _ambiguous(at)
    save(db, user.id, connection.id, rows)
    assert retire_stale_pending(db) == 2

    # The 30-day reconnect backfill reads the window again: the tombstones keep it out.
    _reread_and_backfill(db, user, connection, rows)

    served = _served(client, api_key_header, user)
    assert {s["timestamp"][:10] for s in served} == {
        (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    }
    assert _discard(client, api_key_header, user.id, BODY_GRPID).json() == _discarded_body(at)
