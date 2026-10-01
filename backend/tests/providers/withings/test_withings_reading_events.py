"""The Robin reading event: who gets one and what it says (delivery is Task 8's)."""

import dataclasses
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from celery.exceptions import MaxRetriesExceededError
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.withings_reading_event_task import deliver_withings_reading_event
from app.models import DataSource, SeriesTypeDefinition, User, UserConnection, WithingsMeasureGroupRecord
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.enums import ProviderName, SeriesType, get_series_type_id
from app.services.providers.withings import reading_events
from app.services.providers.withings.measure_groups import ParsedGroup
from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserConnectionFactory, UserFactory
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
    hash_device_id: str | None = None,
) -> ParsedGroup:
    return ParsedGroup(
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=attrib,
        measured_at=_NOW - age,
        metric_keys=metric_keys,
        hash_device_id=hash_device_id,
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
        "pending": False,
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


def test_provisioned_connection_emits_one_per_weigh_in(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_group("1"), _group("2", age=timedelta(minutes=6))], now=_NOW
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
            groups=[_group("1"), _group("2", device_id="dev-2"), _group("3", age=timedelta(minutes=6))],
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
            db,
            user_connection_id=connection.id,
            groups=[_group("1"), _group("2", age=timedelta(minutes=6)), _group("3", age=timedelta(minutes=7))],
            now=_NOW,
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


_POST = f"{_MODULE}.httpx.post"
_TASK_MODULE = "app.integrations.celery.tasks.withings_reading_event_task"


def test_sign_is_hmac_sha256_over_timestamp_dot_body() -> None:
    body = b'{"a":1}'
    expected = hmac.new(b"s3cret", b"1790668800." + body, hashlib.sha256).hexdigest()
    assert reading_events.sign("s3cret", 1790668800, body) == f"t=1790668800,v1={expected}"


def test_sign_shared_vector_pinned_with_robin_backend() -> None:
    """Fixed vector, hard-coded and shared verbatim with the robin-backend verifier test."""
    body = b'{"event":"withings.reading.created","grpid":"1"}'
    digest = "55c83d9f97fc00aa57844f068dfeeb4c00a3a96184db9c93570974bf2a41a877"
    assert reading_events.sign("test-secret", 1790668800, body) == f"t=1790668800,v1={digest}"


def test_sign_matches_robins_verifier_formula() -> None:
    """Robin verifies lowercase-hex HMAC-SHA256(secret, "<t>.<raw body>"); pin it independently."""
    secret, t, body = "whsec_test", 1790668800, b'{"event":"withings.reading.created","grpid":"9"}'
    message = str(t).encode() + b"." + body
    robin_expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    header = reading_events.sign(secret, t, body)
    assert header == f"t={t},v1={robin_expected}"
    assert robin_expected == robin_expected.lower()
    assert len(robin_expected) == 64
    # a different body or timestamp must not verify
    assert reading_events.sign(secret, t + 1, body) != header
    assert reading_events.sign(secret, t, body + b" ") != header


def test_post_event_sends_the_signed_raw_body(enabled: None) -> None:
    payload = reading_events.build_payload(external_user_id="cp", withings_user_id="1", group=_group())
    with patch(_POST, return_value=httpx.Response(200)) as post:
        assert reading_events.post_event(payload, now=1790668800) == "delivered"
    sent_body = post.call_args.kwargs["content"]
    assert json.loads(sent_body) == payload
    headers = post.call_args.kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Robin-Signature"] == reading_events.sign("s3cret", 1790668800, sent_body)
    assert post.call_args.args[0] == "https://robin.example/reading-event"
    assert post.call_args.kwargs["timeout"] == 28.0
    assert post.call_args.kwargs.get("follow_redirects", False) is False


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, "delivered"),
        (202, "delivered"),
        (204, "delivered"),
        (301, "rejected"),
        (302, "rejected"),
        (307, "rejected"),
        (400, "rejected"),
        (401, "rejected"),
        (404, "rejected"),
        (429, "retry"),
        (500, "retry"),
        (502, "retry"),
        (503, "retry"),
    ],
)
def test_post_event_classifies_status(enabled: None, status: int, outcome: str) -> None:
    with patch(_POST, return_value=httpx.Response(status)):
        assert reading_events.post_event({"grpid": "1"}) == outcome


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
def test_post_event_retries_on_network_error(enabled: None, error: Exception) -> None:
    with patch(_POST, side_effect=error):
        assert reading_events.post_event({"grpid": "1"}) == "retry"


def test_post_event_disabled_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", None)
    monkeypatch.setattr(settings, "robin_reading_event_secret", SecretStr("s3cret"))
    with patch(_POST) as post, patch(f"{_MODULE}.log_structured") as log:
        assert reading_events.post_event({"grpid": "1"}) == "disabled"
    post.assert_not_called()
    assert "disabled" in log.call_args.args[2]


def test_post_event_disabled_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", "https://robin.example/reading-event")
    monkeypatch.setattr(settings, "robin_reading_event_secret", None)
    with patch(_POST) as post:
        assert reading_events.post_event({"grpid": "1"}) == "disabled"
    post.assert_not_called()


_BLANK_SETTINGS = [
    pytest.param("", SecretStr("s3cret"), id="empty-url"),
    pytest.param("   ", SecretStr("s3cret"), id="blank-url"),
    pytest.param("https://robin.example/reading-event", SecretStr(""), id="empty-secret"),
    pytest.param("https://robin.example/reading-event", SecretStr("  "), id="blank-secret"),
]


@pytest.mark.parametrize(("url", "secret"), _BLANK_SETTINGS)
def test_an_empty_url_or_secret_counts_as_unset(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    make_provisioned_connection: ProvisionedConnectionMaker,
    url: str,
    secret: SecretStr,
) -> None:
    # An env var set to "" (a common deploy placeholder) must not enable signing with an empty key.
    monkeypatch.setattr(settings, "robin_reading_event_url", url)
    monkeypatch.setattr(settings, "robin_reading_event_secret", secret)
    assert reading_events.is_enabled() is False
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_group()], now=_NOW)
    assert n == 0
    send.assert_not_called()


@pytest.mark.parametrize(("url", "secret"), _BLANK_SETTINGS)
def test_post_event_disabled_with_an_empty_url_or_secret(
    monkeypatch: pytest.MonkeyPatch, url: str, secret: SecretStr
) -> None:
    monkeypatch.setattr(settings, "robin_reading_event_url", url)
    monkeypatch.setattr(settings, "robin_reading_event_secret", secret)
    with patch(_POST) as post:
        assert reading_events.post_event({"grpid": "1"}) == "disabled"
    post.assert_not_called()


def test_a_rejection_is_captured_and_leaks_nothing(enabled: None, caplog: pytest.LogCaptureFixture) -> None:
    payload = {"grpid": "77", "external_user_id": "member-secret-id"}
    with (
        caplog.at_level(logging.DEBUG),
        patch(f"{_MODULE}.log_and_capture_error") as capture,
        patch(_POST, return_value=httpx.Response(401)),
    ):
        assert reading_events.post_event(payload, now=1790668800) == "rejected"
    capture.assert_called_once()
    rendered = caplog.text + repr(capture.call_args)
    assert "member-secret-id" not in rendered
    assert "s3cret" not in rendered
    assert "v1=" not in rendered


def test_network_error_log_leaks_nothing(enabled: None, caplog: pytest.LogCaptureFixture) -> None:
    payload = {"grpid": "77", "external_user_id": "member-secret-id"}
    with caplog.at_level(logging.DEBUG), patch(_POST, side_effect=httpx.ConnectError("down")):
        assert reading_events.post_event(payload) == "retry"
    assert "member-secret-id" not in caplog.text
    assert "s3cret" not in caplog.text


def test_task_is_registered_under_the_enqueued_name() -> None:
    assert deliver_withings_reading_event.name == reading_events.DELIVER_TASK
    assert reading_events.DELIVER_TASK in create_celery().tasks


def test_task_retry_budget_is_three_attempts_total() -> None:
    assert deliver_withings_reading_event.max_retries == 2


@pytest.mark.parametrize(("retries", "countdown"), [(0, 10), (1, 20)])
def test_task_backs_off_exponentially_on_every_retry(retries: int, countdown: int) -> None:
    deliver_withings_reading_event.push_request(retries=retries)
    try:
        with (
            patch(f"{_TASK_MODULE}.post_event", return_value="retry"),
            patch.object(deliver_withings_reading_event, "retry", side_effect=RuntimeError("retrying")) as retry,
            pytest.raises(RuntimeError, match="retrying"),
        ):
            deliver_withings_reading_event.run({"grpid": "1"})
    finally:
        deliver_withings_reading_event.pop_request()
    assert retry.call_args.kwargs["countdown"] == countdown


def test_task_gives_up_after_the_third_attempt() -> None:
    deliver_withings_reading_event.push_request(retries=2)
    try:
        with (
            patch(f"{_TASK_MODULE}.post_event", return_value="retry") as post_event,
            patch(f"{_TASK_MODULE}.log_and_capture_error") as capture,
            pytest.raises(MaxRetriesExceededError),
        ):
            deliver_withings_reading_event.run({"grpid": "1", "external_user_id": "member-secret-id"})
    finally:
        deliver_withings_reading_event.pop_request()
    post_event.assert_called_once()
    capture.assert_called_once()
    assert "member-secret-id" not in repr(capture.call_args)


def test_three_attempts_in_total_against_a_down_robin(enabled: None) -> None:
    """Drive the real task through attempts 0..2 against a 503 Robin: exactly 3 POSTs, then it fails."""
    with patch(_POST, return_value=httpx.Response(503)) as post, patch(f"{_TASK_MODULE}.log_and_capture_error"):
        for attempt in range(3):
            deliver_withings_reading_event.push_request(retries=attempt)
            try:
                with (
                    patch.object(deliver_withings_reading_event, "retry", side_effect=RuntimeError("again")),
                    pytest.raises(RuntimeError if attempt < 2 else MaxRetriesExceededError),
                ):
                    deliver_withings_reading_event.run({"grpid": "1"})
            finally:
                deliver_withings_reading_event.pop_request()
    assert post.call_count == 3


@pytest.mark.parametrize("outcome", ["delivered", "rejected", "disabled"])
def test_task_does_not_retry_terminal_outcomes(outcome: str) -> None:
    with (
        patch(f"{_TASK_MODULE}.post_event", return_value=outcome),
        patch.object(deliver_withings_reading_event, "retry") as retry,
    ):
        assert deliver_withings_reading_event.run({"grpid": "1"}) == {"outcome": outcome}
    retry.assert_not_called()


# --- One weigh-in = one event ---------------------------------------------------------------------
# A cellular Body Pro 2 weigh-in, as seen on staging (2026-10-01): two groups, same date and device.
_HASH = "41a451ad0c5e7f2b9d3a6c8e1f4b7a2d5c8e0f3a"
_BODY_KEYS = (
    "weight",
    "fat_ratio",
    "fat_mass",
    "muscle_mass",
    "hydration",
    "bone_mass",
    "visceral_fat",
    "basal_metabolic_rate",
)


def _body(grpid: str = "8530283247", **kw: Any) -> ParsedGroup:
    return _group(grpid, device_id="15542329", hash_device_id=_HASH, metric_keys=_BODY_KEYS, **kw)


def _pulse(grpid: str = "8530283250", **kw: Any) -> ParsedGroup:
    return _group(grpid, device_id="15542329", hash_device_id=_HASH, metric_keys=("heart_rate",), **kw)


def _record(db: Session, connection: UserConnection, group: ParsedGroup, series: tuple[SeriesType, ...] = ()) -> None:
    """``group`` as an earlier ingest left it: its withings_measure_group row and samples."""
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=connection.user_id,
            user_connection_id=connection.id,
            grpid=group.grpid,
            device_id=group.device_id,
            hash_device_id=group.hash_device_id,
            model=group.model,
            attrib=group.attrib,
            measured_at=group.measured_at,
            status=group.status,
        )
    )
    source = db.query(DataSource).filter(DataSource.user_id == connection.user_id).one_or_none()
    if source is None:
        source = DataSourceFactory(
            user=db.get(User, connection.user_id),
            provider=ProviderName.WITHINGS,
            device_model=None,
            source="withings",
            device_type=None,
        )
    for s in series:
        DataPointSeriesFactory(
            data_source=source,
            series_type=db.get(SeriesTypeDefinition, get_series_type_id(s)),
            value=Decimal("1"),
            recorded_at=group.measured_at,
            external_id=group.grpid,
        )
    db.flush()


def test_a_weigh_in_in_two_groups_is_one_event_with_the_heart_rate_in_it(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_body(), _pulse()], now=_NOW
        )
    assert n == 1
    (payload,) = _sent(send)
    assert payload["grpid"] == "8530283247"
    assert payload["types"] == [*_BODY_KEYS, "heart_rate"]
    assert payload["hash_deviceid"] == _HASH
    assert payload["device_id"] == "15542329"


def test_the_weight_group_announces_the_weigh_in_whatever_the_batch_order(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_pulse("8530283240"), _body()], now=_NOW
        )
    assert n == 1
    (payload,) = _sent(send)
    assert payload["grpid"] == "8530283247"
    assert set(payload["types"]) == {*_BODY_KEYS, "heart_rate"}


def test_two_weigh_ins_in_one_batch_are_two_events(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    later = timedelta(minutes=1)
    groups = [_body(), _pulse(), _body("8530358979", age=later), _pulse("8530358986", age=later)]
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=groups, now=_NOW)
    assert n == 2
    assert [p["grpid"] for p in _sent(send)] == ["8530283247", "8530358979"]


def test_a_pulse_group_after_its_weigh_in_was_announced_sends_nothing(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    _record(db, connection, _body(), (SeriesType.weight, SeriesType.body_fat_percentage))
    pulse = _pulse()
    _record(db, connection, pulse, (SeriesType.heart_rate,))  # this batch's own row, as record_new_groups leaves it
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[pulse], now=_NOW)
    assert n == 0
    send.assert_not_called()


def test_a_lone_pulse_group_is_still_announced(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    pulse = _pulse()
    _record(db, connection, pulse, (SeriesType.heart_rate,))
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[pulse], now=_NOW)
    assert n == 1
    assert _sent(send)[0]["grpid"] == "8530283250"
    assert _sent(send)[0]["types"] == ["heart_rate"]


def test_an_earlier_sibling_with_nothing_to_announce_does_not_silence_the_weigh_in(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """A sibling with no C2 sample (or a manual one) was never announced, so this batch still is."""
    connection = _provisioned(db, make_provisioned_connection)
    _record(db, connection, _group("8530283240", device_id="15542329", hash_device_id=_HASH, metric_keys=()))
    _record(db, connection, _pulse("8530283241", attrib=2), (SeriesType.heart_rate,))
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_body()], now=_NOW)
    assert n == 1
    assert _sent(send)[0]["grpid"] == "8530283247"


def test_an_earlier_group_of_another_device_or_time_does_not_silence_the_weigh_in(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    _, other = make_provisioned_connection()
    _record(db, connection, _group("1", device_id="15542329", hash_device_id="another-hash"), (SeriesType.weight,))
    _record(db, connection, _body("2", age=timedelta(minutes=6)), (SeriesType.weight,))
    _record(db, other, _body("3"), (SeriesType.weight,))
    with patch(_SEND):
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[_pulse()], now=_NOW)
    assert n == 1


# --- pending (spec 2026-10-01 D1, contract A3) ----------------------------------------------------


def test_a_pending_weigh_in_is_announced_once_with_pending_true(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    groups = [dataclasses.replace(_body(), status="pending"), dataclasses.replace(_pulse(), status="pending")]
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=groups, now=_NOW)
    assert n == 1
    (payload,) = _sent(send)
    assert payload["pending"] is True
    assert payload["grpid"] == "8530283247"
    assert payload["types"] == [*_BODY_KEYS, "heart_rate"]


def test_a_registered_weigh_in_is_announced_with_pending_false(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    connection = _provisioned(db, make_provisioned_connection)
    with patch(_SEND) as send:
        reading_events.enqueue_new_reading_events(
            db, user_connection_id=connection.id, groups=[_body(), _pulse()], now=_NOW
        )
    assert [p["pending"] for p in _sent(send)] == [False]


def test_a_discarded_group_is_never_announced(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """A late sibling of a weigh-in the member already discarded is recorded as a tombstone, silently."""
    connection = _provisioned(db, make_provisioned_connection)
    groups = [dataclasses.replace(_pulse(), status="discarded")]
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=groups, now=_NOW)
    assert n == 0
    send.assert_not_called()


def test_a_late_sibling_of_a_pending_session_is_not_announced_again(
    db: Session, enabled: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """A pending sibling has no samples, so it is its status that says the weigh-in was announced."""
    connection = _provisioned(db, make_provisioned_connection)
    _record(db, connection, dataclasses.replace(_body(), status="pending"))
    pulse = dataclasses.replace(_pulse(), status="pending")
    _record(db, connection, pulse)  # this batch's own row, as record_new_groups leaves it
    with patch(_SEND) as send:
        n = reading_events.enqueue_new_reading_events(db, user_connection_id=connection.id, groups=[pulse], now=_NOW)
    assert n == 0
    send.assert_not_called()
