"""Ingest of an ambiguous (attrib 1) weigh-in on the scale we sold: held pending, raw kept, no samples.

Spec 2026-10-01 §4.1 "Ingest", D1, D9; rulings R3-R5, R8 of the OW plan.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import null
from sqlalchemy.orm import Session

from app.models import WithingsMeasureGroupRecord
from app.services.providers.withings.data_247 import raw_group
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import (
    BODY_GRPID,
    PULSE_GRPID,
    SEND,
    body_group,
    enable_events,
    pulse_group,
    raw_is_null,
    recent,
    records,
    samples_at,
    save,
)


def test_an_ambiguous_weigh_in_is_held_pending_with_no_samples(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()
    rows = [body_group(at), pulse_group(at)]
    with patch(SEND) as send:
        counts = save(db, user.id, connection.id, rows)

    assert counts.inserted == 0
    assert samples_at(db, user.id, at) == []
    stored = records(db, connection.id)
    assert {grpid: r.status for grpid, r in stored.items()} == {str(BODY_GRPID): "pending", str(PULSE_GRPID): "pending"}
    assert stored[str(BODY_GRPID)].raw == rows[0]  # as received, every field kept
    assert stored[str(PULSE_GRPID)].raw == rows[1]
    (call,) = send.call_args_list
    payload = call.kwargs["args"][0]
    assert payload["pending"] is True
    assert payload["grpid"] == str(BODY_GRPID)
    assert "weight" in payload["types"]
    assert "heart_rate" in payload["types"]


def test_only_the_ambiguous_session_is_held(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    held, sure = recent(), recent() + 60
    counts = save(db, user.id, connection.id, [body_group(held), body_group(sure, grpid=8530358979, attrib=0)])
    assert samples_at(db, user.id, held) == []
    assert len(samples_at(db, user.id, sure)) == 4
    assert counts.inserted == 4
    stored = records(db, connection.id)
    assert stored["8530358979"].status == "registered"
    assert raw_is_null(db, connection.id) == 1  # only the registered group


def test_a_reread_of_a_pending_weigh_in_writes_nothing_new(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()
    with patch(SEND) as send:
        save(db, user.id, connection.id, [body_group(at), pulse_group(at)])
        save(db, user.id, connection.id, [body_group(at), pulse_group(at)])
    assert send.call_count == 1
    assert samples_at(db, user.id, at) == []
    assert {r.status for r in records(db, connection.id).values()} == {"pending"}


def test_a_late_pulse_group_joins_the_pending_weigh_in(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()
    with patch(SEND) as send:
        save(db, user.id, connection.id, [body_group(at)])
        # The pulse group comes later and unflagged: it is still part of the ambiguous weigh-in.
        save(db, user.id, connection.id, [body_group(at), pulse_group(at, attrib=0)])
    assert send.call_count == 1
    assert samples_at(db, user.id, at) == []
    late = records(db, connection.id)[str(PULSE_GRPID)]
    assert late.status == "pending"
    assert late.raw == pulse_group(at, attrib=0)


def test_a_reread_of_a_discarded_weigh_in_writes_no_samples(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    at = recent()
    rows = [body_group(at, attrib=0), pulse_group(at, attrib=0)]
    save(db, user.id, connection.id, rows)
    assert len(samples_at(db, user.id, at)) == 5
    # As a discard leaves it (Task 6 does this for real): tombstones and no samples.
    record = WithingsMeasureGroupRecord
    db.query(record).filter(record.user_connection_id == connection.id).update({"status": "discarded", "raw": null()})
    for sample in samples_at(db, user.id, at):
        db.delete(sample)
    db.flush()

    save(db, user.id, connection.id, rows)

    assert samples_at(db, user.id, at) == []


def test_ambiguous_on_a_self_linked_account_is_registered_as_today(db: Session) -> None:
    user = UserFactory()
    own = UserConnectionFactory(user=user, provider="withings")  # no withings_sdk_account row
    at = recent()
    save(db, user.id, own.id, [body_group(at), pulse_group(at)])
    assert len(samples_at(db, user.id, at)) == 5
    assert {r.status for r in records(db, own.id).values()} == {"registered"}
    assert raw_is_null(db, own.id) == 2


def test_the_held_raw_carries_the_response_timezone_when_the_group_has_none(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    at = recent()
    save(db, user.id, connection.id, [body_group(at, timezone=None)], envelope={"timezone": "America/New_York"})
    assert records(db, connection.id)[str(BODY_GRPID)].raw == body_group(at, timezone="America/New_York")


def test_raw_group_keeps_the_groups_own_timezone_and_fills_only_a_missing_one() -> None:
    assert raw_group({"date": 1, "timezone": "Europe/Rome"}, "America/New_York") == {
        "date": 1,
        "timezone": "Europe/Rome",
    }
    assert raw_group({"date": 1}, "America/New_York") == {"date": 1, "timezone": "America/New_York"}
    assert raw_group({"date": 1, "timezone": ""}, "UTC") == {"date": 1, "timezone": "UTC"}
    assert raw_group({"date": 1}, None) == {"date": 1}
