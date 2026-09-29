"""The Robin reading event: who gets one and what it says (delivery is Task 8's)."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import UserConnection
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from app.services.providers.withings import reading_events
from app.services.providers.withings.measure_groups import ParsedGroup
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker

_NOW = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
_MODULE = "app.services.providers.withings.reading_events"
_SEND = f"{_MODULE}.celery_app.send_task"
_SYNC = f"{_MODULE}.sync_devices_from_withings"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", "https://robin.example/reading-event")
    monkeypatch.setattr(settings, "robin_reading_event_secret", SecretStr("s3cret"))


def _group(
    grpid: str = "1",
    *,
    device_id: str | None = "dev-1",
    attrib: int | None = 0,
    age: timedelta = timedelta(minutes=5),
    metric_keys: tuple[str, ...] = ("weight", "fat_ratio"),
) -> ParsedGroup:
    return ParsedGroup(
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=attrib,
        measured_at=_NOW - age,
        metric_keys=metric_keys,
    )


def _provisioned(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> UserConnection:
    _, connection = make_provisioned_connection()
    connection.provider_user_id = "4242"
    db.flush()
    return connection


def _external_id(db: Session, connection_id: UUID) -> str:
    query = db.query(WithingsSdkAccount.external_id)
    return query.filter(WithingsSdkAccount.user_connection_id == connection_id).scalar()


def _add_device(
    db: Session,
    connection_id: UUID,
    device_id: str = "dev-1",
    hash_device_id: str | None = "hash-1",
    *,
    listed: bool = False,
) -> None:
    db.add(
        WithingsDevice(
            id=uuid4(),
            user_connection_id=connection_id,
            device_id=device_id,
            hash_device_id=hash_device_id,
            last_getdevice_at=_NOW if listed else None,
            updated_at=_NOW,
        )
    )
    db.flush()


def _sent(send: MagicMock) -> list[dict[str, Any]]:
    return [c.kwargs["args"][0] for c in send.call_args_list]


def test_build_payload_matches_the_contract() -> None:
    payload = reading_events.build_payload(
        external_user_id="cp-123", withings_user_id="4242", group=_group("77"), hash_deviceid="hash-1"
    )
    assert payload == {
        "event": "withings.reading.created",
        "external_user_id": "cp-123",
        "withings_user_id": "4242",
        "device_id": "dev-1",
        "hash_deviceid": "hash-1",
        "grpid": "77",
        "measured_at": "2026-09-29T07:55:00Z",
        "types": ["weight", "fat_ratio"],
    }


def test_unset_url_sends_nothing(
    db: Session, monkeypatch: pytest.MonkeyPatch, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", None)
    monkeypatch.setattr(settings, "robin_reading_event_secret", SecretStr("s3cret"))
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert n == 0
    send.assert_not_called()


def test_unset_secret_sends_nothing(
    db: Session, monkeypatch: pytest.MonkeyPatch, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", "https://robin.example/reading-event")
    monkeypatch.setattr(settings, "robin_reading_event_secret", None)
    assert reading_events.is_enabled() is False
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    send.assert_not_called()


def test_provisioned_connection_emits_one_per_group(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group("1"), _group("2")], now=_NOW
        )
    assert n == 2
    sent = _sent(send)
    assert [p["grpid"] for p in sent] == ["1", "2"]
    assert {p["external_user_id"] for p in sent} == {_external_id(db, connection.id)}
    assert {p["withings_user_id"] for p in sent} == {"4242"}
    assert {p["hash_deviceid"] for p in sent} == {None}  # device never swept, no oauth to sweep with
    assert send.call_args.args[0] == reading_events.DELIVER_TASK


def test_event_carries_the_stored_hash_deviceid(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    _add_device(db, connection.id)
    with patch(_SEND) as send, patch(_SYNC) as sync:
        reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group()], now=_NOW, oauth=MagicMock()
        )
    assert send.call_args.kwargs["args"][0]["hash_deviceid"] == "hash-1"
    sync.assert_not_called()  # hash already known: no Getdevice call


def test_a_missing_hash_refreshes_getdevice_once_per_call(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    oauth = MagicMock()

    def _getdevice(db: Session, **_: Any) -> list[WithingsDevice]:
        _add_device(db, connection.id, "dev-1", "hash-1")
        _add_device(db, connection.id, "dev-2", "hash-2")
        return []

    with patch(_SEND) as send, patch(_SYNC, side_effect=_getdevice) as sync:
        n = reading_events.enqueue_new_reading_events(
            db,
            user_connection_id=connection.id,
            groups=[_group("1"), _group("2", device_id="dev-2"), _group("3")],
            now=_NOW,
            oauth=oauth,
        )
    assert n == 3
    sync.assert_called_once()
    assert sync.call_args.kwargs == {"user_id": user.id, "oauth": oauth, "connection_id": connection.id}
    assert [p["hash_deviceid"] for p in _sent(send)] == ["hash-1", "hash-2", "hash-1"]


def test_a_failed_refresh_still_emits_with_a_null_hash(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    _add_device(db, connection.id, hash_device_id=None)  # row present, hash never reported
    with (
        patch(_SEND) as send,
        patch(_SYNC, side_effect=RuntimeError("withings down")) as sync,
        patch(f"{_MODULE}.log_and_capture_error") as capture,
    ):
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group()], now=_NOW, oauth=MagicMock()
        )
    assert n == 1
    sync.assert_called_once()
    capture.assert_called_once()
    assert send.call_args.kwargs["args"][0]["hash_deviceid"] is None


def test_self_linked_connection_never_emits(db: Session, enabled: None) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")  # no sdk account row
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert n == 0
    send.assert_not_called()


def test_old_groups_do_not_emit(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(
            db,
            user_connection_id=connection.id,
            groups=[_group("old", age=timedelta(hours=25)), _group("fresh", age=timedelta(hours=23))],
            now=_NOW,
        )
    assert n == 1
    assert send.call_args.kwargs["args"][0]["grpid"] == "fresh"


def test_manual_deviceless_and_non_c2_groups_do_not_emit(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send, patch(_SYNC) as sync:
        n = reading_events.enqueue_new_reading_events(
            db,
            user_connection_id=connection.id,
            groups=[
                _group("manual", attrib=2),
                _group("typed", attrib=4),
                _group("nodev", device_id=None),
                _group("bp-only", metric_keys=()),
            ],
            now=_NOW,
            oauth=MagicMock(),
        )
    assert n == 0
    send.assert_not_called()
    sync.assert_not_called()  # nothing eligible, so no Getdevice call either


def test_enqueue_error_is_swallowed(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with (
        patch(_SEND, side_effect=ConnectionError("redis down")),
        patch(f"{_MODULE}.log_and_capture_error") as capture,
    ):
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert n == 0
    capture.assert_called_once()


def test_one_failed_enqueue_does_not_drop_the_rest_of_the_batch(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with (
        patch(_SEND, side_effect=[None, ConnectionError("redis blip"), None]) as send,
        patch(f"{_MODULE}.log_and_capture_error") as capture,
    ):
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group("1"), _group("2"), _group("3")], now=_NOW
        )
    assert n == 2
    assert [p["grpid"] for p in _sent(send)] == ["1", "2", "3"]
    capture.assert_called_once()
    assert capture.call_args.kwargs["extra"]["grpid"] == "2"


def test_a_failed_setup_query_leaves_the_session_usable(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)

    def _broken_query(db: Session, *_: Any) -> None:
        db.execute(text("SELECT * FROM no_such_table"))  # aborts the transaction, as a real DB error does

    with (
        patch(_SEND) as send,
        patch(f"{_MODULE}.UserConnectionRepository.get", side_effect=_broken_query),
        patch(f"{_MODULE}.log_and_capture_error") as capture,
    ):
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert n == 0
    send.assert_not_called()
    capture.assert_called_once()
    assert db.execute(select(1)).scalar() == 1  # without the rollback: InFailedSqlTransaction


def test_a_device_getdevice_listed_without_a_hash_is_not_refreshed(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    _add_device(db, connection.id, hash_device_id=None, listed=True)  # Getdevice saw it, no hash reported
    with patch(_SEND) as send, patch(_SYNC) as sync:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group()], now=_NOW, oauth=MagicMock()
        )
    assert n == 1
    sync.assert_not_called()
    assert send.call_args.kwargs["args"][0]["hash_deviceid"] is None


def test_a_missing_withings_userid_is_sent_as_null(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    _, connection = make_provisioned_connection()
    connection.provider_user_id = None
    db.flush()
    with patch(_SEND) as send:
        reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert send.call_args.kwargs["args"][0]["withings_user_id"] is None
