"""Confirm and discard a weigh-in (spec 2026-10-01 D1-D5, D9; contract A2), against the real ingest path."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, User, UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.attribution import DiscardResult, confirm_reading, discard_reading
from app.services.providers.withings.readings import get_reading, list_device_readings
from tests.factories import UserConnectionFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import (
    BODY_GRPID,
    HASH,
    PULSE_GRPID,
    SEND,
    body_group,
    enable_events,
    pulse_group,
    raw_is_null,
    recent,
    records,
    samples_at,
    samples_of,
    save,
)

_BODY, _PULSE = str(BODY_GRPID), str(PULSE_GRPID)


def _ambiguous_weigh_in(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> tuple[User, UserConnection, int]:
    user, connection = make_provisioned_connection()
    at = recent()
    save(db, user.id, connection.id, [body_group(at), pulse_group(at)])
    return user, connection, at


# --- confirm -------------------------------------------------------------------------------------


def test_confirm_writes_exactly_the_samples_ingest_would_have(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Same rows, one member held pending then confirmed, the other sure from the start: same samples.

    The groups carry no timezone of their own, so the zone offset comes from the response body:
    the held payload must have kept it.
    """
    at = recent()
    envelope = {"timezone": "America/New_York"}
    held_user, held_connection = make_provisioned_connection()
    sure_user, sure_connection = make_provisioned_connection()
    save(
        db,
        held_user.id,
        held_connection.id,
        [body_group(at, timezone=None), pulse_group(at, timezone=None)],
        envelope=envelope,
    )
    rows = [body_group(at, attrib=0, timezone=None), pulse_group(at, attrib=0, timezone=None)]
    save(db, sure_user.id, sure_connection.id, rows, envelope=envelope)
    assert samples_of(db, held_user.id) == set()

    confirm_reading(db, user_id=held_user.id, grpid=_BODY)

    assert samples_of(db, held_user.id) == samples_of(db, sure_user.id)
    assert len(samples_of(db, held_user.id)) == 5
    # New York's offset, DST or not (recent() decides which), and never UTC's.
    assert {s[4] for s in samples_of(db, held_user.id)} <= {"-04:00", "-05:00"}
    # One data source, as ingest uses: confirm wrote with the same provider/source identity.
    assert db.query(DataSource).filter(DataSource.user_id == held_user.id).count() == 1


def test_confirm_keeps_the_numbers_the_member_saw(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection, _ = _ambiguous_weigh_in(db, make_provisioned_connection)
    before = get_reading(db, user_id=user.id, grpid=_BODY)
    after = confirm_reading(db, user_id=user.id, grpid=_BODY)
    assert before is not None
    assert after is not None
    assert before.status == "pending"
    assert after.status == "registered"
    assert after.metrics == before.metrics
    assert after.metrics["bone_mass"] == 2.735
    assert {r.status for r in records(db, connection.id).values()} == {"registered"}
    assert raw_is_null(db, connection.id) == 2


def test_confirm_by_a_sibling_grpid_confirms_the_whole_weigh_in(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection, at = _ambiguous_weigh_in(db, make_provisioned_connection)
    reading = confirm_reading(db, user_id=user.id, grpid=_PULSE)
    assert reading is not None
    assert reading.grpid == _BODY
    assert reading.metrics["heart_rate"] == 64.0
    assert len(samples_at(db, user.id, at)) == 5
    assert {r.status for r in records(db, connection.id).values()} == {"registered"}


def test_confirm_is_idempotent(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, _, at = _ambiguous_weigh_in(db, make_provisioned_connection)
    first = confirm_reading(db, user_id=user.id, grpid=_BODY)
    second = confirm_reading(db, user_id=user.id, grpid=_BODY)
    assert second == first
    assert len(samples_at(db, user.id, at)) == 5


def test_confirm_of_a_registered_reading_returns_it_unchanged(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    at = recent()
    save(db, user.id, connection.id, [body_group(at, attrib=0), pulse_group(at, attrib=0)])
    before = samples_of(db, user.id)
    reading = confirm_reading(db, user_id=user.id, grpid=_BODY)
    assert reading == get_reading(db, user_id=user.id, grpid=_BODY)
    assert samples_of(db, user.id) == before


def test_confirm_answers_none_for_a_missing_discarded_or_foreign_reading(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, _, _ = _ambiguous_weigh_in(db, make_provisioned_connection)
    stranger, _ = make_provisioned_connection()
    assert confirm_reading(db, user_id=user.id, grpid="404") is None
    assert confirm_reading(db, user_id=stranger.id, grpid=_BODY) is None
    discard_reading(db, user_id=user.id, grpid=_BODY)
    assert confirm_reading(db, user_id=user.id, grpid=_BODY) is None
    assert samples_of(db, user.id) == set()


def test_confirm_ignores_the_members_own_account(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, _ = make_provisioned_connection()
    own = UserConnectionFactory(user=user, provider="withings")
    save(db, user.id, own.id, [body_group(recent(), grpid=777)])
    assert confirm_reading(db, user_id=user.id, grpid="777") is None


# --- discard -------------------------------------------------------------------------------------


def test_discard_of_a_pending_weigh_in(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, connection, at = _ambiguous_weigh_in(db, make_provisioned_connection)
    result = discard_reading(db, user_id=user.id, grpid=_PULSE)
    assert result == DiscardResult(
        grpid=_BODY,
        grpids=[_BODY, _PULSE],
        measured_at=datetime.fromtimestamp(at, tz=timezone.utc),
        was="pending",
    )
    assert {r.status for r in records(db, connection.id).values()} == {"discarded"}
    assert raw_is_null(db, connection.id) == 2
    assert get_reading(db, user_id=user.id, grpid=_BODY) is None
    assert list_device_readings(db, user_id=user.id, device_id=HASH).items == []


def test_discard_of_a_registered_weigh_in_deletes_only_its_samples(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection = make_provisioned_connection()
    stranger, stranger_connection = make_provisioned_connection()
    first, second = recent(), recent() + 60
    rows = [body_group(first, attrib=0), pulse_group(first, attrib=0)]
    save(db, user.id, connection.id, [*rows, body_group(second, grpid=8530358979, attrib=0)])
    save(db, stranger.id, stranger_connection.id, rows)  # the same grpids on another member's account

    result = discard_reading(db, user_id=user.id, grpid=_BODY)

    assert result is not None
    assert result.was == "registered"
    assert samples_at(db, user.id, first) == []
    assert len(samples_at(db, user.id, second)) == 4
    assert len(samples_at(db, stranger.id, first)) == 5
    assert [r.grpid for r in list_device_readings(db, user_id=user.id, device_id=HASH).items] == ["8530358979"]


def test_discard_is_idempotent(db: Session, make_provisioned_connection: ProvisionedConnectionMaker) -> None:
    user, _, _ = _ambiguous_weigh_in(db, make_provisioned_connection)
    first = discard_reading(db, user_id=user.id, grpid=_BODY)
    second = discard_reading(db, user_id=user.id, grpid=_PULSE)
    assert first is not None
    assert second is not None
    assert second.was == "discarded"
    # The weight group is the lowest grpid, so the fallback representative (ruling R9) is the same.
    assert (second.grpid, second.grpids, second.measured_at) == (first.grpid, first.grpids, first.measured_at)


def test_discard_after_confirm_deletes_the_confirmed_samples(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, _, at = _ambiguous_weigh_in(db, make_provisioned_connection)
    confirm_reading(db, user_id=user.id, grpid=_BODY)
    assert len(samples_at(db, user.id, at)) == 5
    result = discard_reading(db, user_id=user.id, grpid=_BODY)
    assert result is not None
    assert result.was == "registered"
    assert samples_at(db, user.id, at) == []


def test_discard_of_a_missing_or_foreign_reading_is_none(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, connection, _ = _ambiguous_weigh_in(db, make_provisioned_connection)
    stranger, _ = make_provisioned_connection()
    assert discard_reading(db, user_id=user.id, grpid="404") is None
    assert discard_reading(db, user_id=stranger.id, grpid=_BODY) is None
    assert {r.status for r in records(db, connection.id).values()} == {"pending"}


def test_a_discarded_reading_is_not_resurrected_by_a_reread(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()  # one date for both groups: one session
    rows = [body_group(at, attrib=0), pulse_group(at, attrib=0)]
    with patch(SEND) as send:
        save(db, user.id, connection.id, rows)
        discard_reading(db, user_id=user.id, grpid=_BODY)
        save(db, user.id, connection.id, rows)
    assert samples_of(db, user.id) == set()
    assert list_device_readings(db, user_id=user.id, device_id=HASH).items == []
    assert send.call_count == 1  # the first ingest only


@pytest.mark.parametrize("confirm_first", [False, True])
def test_discard_still_finds_a_session_whose_connection_was_revoked(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, confirm_first: bool
) -> None:
    """Contract A2b.1: a repeat discard answers 200 for any session that ever existed, even after revocation."""
    user, connection, at = _ambiguous_weigh_in(db, make_provisioned_connection)
    if confirm_first:
        confirm_reading(db, user_id=user.id, grpid=_BODY)
        assert len(samples_at(db, user.id, at)) == 5
    else:
        discard_reading(db, user_id=user.id, grpid=_BODY)
    connection.status = ConnectionStatus.REVOKED
    db.commit()

    result = discard_reading(db, user_id=user.id, grpid=_PULSE)

    assert result is not None
    assert result.was == ("registered" if confirm_first else "discarded")
    assert result.grpids == [_BODY, _PULSE]
    assert samples_at(db, user.id, at) == []
    assert {r.status for r in records(db, connection.id).values()} == {"discarded"}
    # Confirm, like the list and detail, serves active connections only.
    assert confirm_reading(db, user_id=user.id, grpid=_BODY) is None
