"""One member, several connections with one provider: what the schema and the lookups promise.

A Withings cellular device cannot be activated onto an account the partner did not create, so a
member who has linked their own Withings account and is then shipped a device holds two — and
another for every later order. ``ix_user_connection_user_provider`` used to forbid that outright.

These are the constraint's edges and the primary-connection rule that seventeen call sites
depend on. Real session, because every assertion here is about what Postgres accepts and what
comes back out of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from tests.factories import UserFactory

repo = UserConnectionRepository()

_EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)
_LATER = _EARLIER + timedelta(days=30)


def _add(
    db: Session,
    user_id: UUID,
    provider_user_id: str | None,
    *,
    provider: str = "withings",
    status: ConnectionStatus = ConnectionStatus.ACTIVE,
    created_at: datetime = _EARLIER,
    access_token: str = "token",
) -> UserConnection:
    connection = UserConnection(
        id=uuid4(),
        user_id=user_id,
        provider=provider,
        provider_user_id=provider_user_id,
        access_token=access_token,
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(connection)
    db.flush()
    return connection


class TestTheRelaxedUniqueIndex:
    def test_two_withings_accounts_for_one_member_are_allowed(self, db: Session) -> None:
        user = UserFactory()

        _add(db, user.id, "withings-personal")
        _add(db, user.id, "withings-provisioned")

        db.flush()
        assert db.query(UserConnection).filter(UserConnection.user_id == user.id).count() == 2

    def test_the_same_account_twice_for_one_member_is_rejected(self, db: Session) -> None:
        # Relaxing the index did not make it meaningless: two rows describing ONE Withings
        # account is still a bug, and this is the guard if Withings ever adopts an existing
        # account rather than creating one.
        user = UserFactory()
        _add(db, user.id, "withings-same")

        # Inside the raises block: ``_add`` flushes, so this is where the constraint fires.
        with pytest.raises(IntegrityError):
            _add(db, user.id, "withings-same")

    def test_two_connections_with_no_account_id_are_rejected(self, db: Session) -> None:
        # NULLS NOT DISTINCT, and the reason it is not decoration. provider_user_id is nullable
        # and SDK-based providers (Apple) never set one; under Postgres' default, NULLs are
        # distinct in a unique index, so those connections would have lost their uniqueness
        # guarantee entirely — a provider with nothing to do with Withings, broken by a
        # Withings change.
        user = UserFactory()
        _add(db, user.id, None, provider="apple")

        with pytest.raises(IntegrityError):
            _add(db, user.id, None, provider="apple")

    def test_the_same_account_on_two_members_is_allowed(self, db: Session) -> None:
        # The inverse fan-out, which has always been supported: one provider account shared by
        # several OW profiles. Untouched by this change, and asserted so it stays that way.
        first = UserFactory()
        second = UserFactory()

        _add(db, first.id, "withings-shared")
        _add(db, second.id, "withings-shared")

        db.flush()
        assert db.query(UserConnection).filter(UserConnection.provider_user_id == "withings-shared").count() == 2


class TestPrimaryConnectionLookup:
    def test_returns_the_oldest_active_connection(self, db: Session) -> None:
        # The documented rule, and the reason it is oldest-first: a member's own linked account
        # almost always predates a device we shipped them, so it stays primary.
        user = UserFactory()
        personal = _add(db, user.id, "withings-personal", created_at=_EARLIER)
        _add(db, user.id, "withings-provisioned", created_at=_LATER)

        assert repo.get_by_user_and_provider(db, user.id, "withings").id == personal.id
        assert repo.get_active_connection(db, user.id, "withings").id == personal.id

    def test_does_not_raise_when_a_member_has_several(self, db: Session) -> None:
        # Both lookups ended in .one_or_none(), which raises MultipleResultsFound as soon as a
        # second row exists — i.e. every Withings call site would have started throwing the
        # moment the index was relaxed. Deleting the .order_by/.first() and restoring
        # one_or_none is what this catches.
        user = UserFactory()
        _add(db, user.id, "withings-personal")
        _add(db, user.id, "withings-provisioned")

        assert repo.get_by_user_and_provider(db, user.id, "withings") is not None
        assert repo.get_active_connection(db, user.id, "withings") is not None

    def test_prefers_an_active_connection_over_an_older_revoked_one(self, db: Session) -> None:
        user = UserFactory()
        _add(db, user.id, "withings-old", status=ConnectionStatus.REVOKED, created_at=_EARLIER)
        live = _add(db, user.id, "withings-live", created_at=_LATER)

        assert repo.get_by_user_and_provider(db, user.id, "withings").id == live.id

    def test_still_returns_a_revoked_connection_when_it_is_the_only_one(self, db: Session) -> None:
        # Ordered, not filtered. base_oauth._save_connection looks a connection up in order to
        # REACTIVATE it; filtering revoked rows out would make it miss the row it means to
        # revive and create a duplicate instead.
        user = UserFactory()
        revoked = _add(db, user.id, "withings-revoked", status=ConnectionStatus.REVOKED)

        assert repo.get_by_user_and_provider(db, user.id, "withings").id == revoked.id
        assert repo.get_active_connection(db, user.id, "withings") is None


class TestLookupByProviderAccount:
    def test_finds_the_named_account_not_the_primary_one(self, db: Session) -> None:
        # What the OAuth callback asks. Asking get_by_user_and_provider instead would return
        # the member's PRIMARY connection — quite possibly the device account we provisioned —
        # and the callback would overwrite ITS tokens with the personal account's.
        user = UserFactory()
        _add(db, user.id, "withings-personal", created_at=_EARLIER)
        provisioned = _add(db, user.id, "withings-provisioned", created_at=_LATER)

        found = repo.get_by_user_provider_and_account(db, user.id, "withings", "withings-provisioned")

        assert found is not None
        assert found.id == provisioned.id

    def test_an_unknown_account_is_not_found_rather_than_falling_back(self, db: Session) -> None:
        # A member linking a THIRD Withings account must be a create, not an overwrite of
        # whichever row happened to sort first.
        user = UserFactory()
        _add(db, user.id, "withings-personal")

        assert repo.get_by_user_provider_and_account(db, user.id, "withings", "withings-brand-new") is None

    def test_a_provider_with_no_account_id_falls_back_to_the_primary(self, db: Session) -> None:
        # Providers that report no account id can only ever have one row per member — the index
        # is NULLS NOT DISTINCT — so the two questions coincide and today's behaviour is kept.
        user = UserFactory()
        apple = _add(db, user.id, None, provider="apple")

        found = repo.get_by_user_provider_and_account(db, user.id, "apple", None)

        assert found is not None
        assert found.id == apple.id


class TestConnectionScopedDisconnect:
    def test_revokes_only_the_named_connection(self, db: Session) -> None:
        user = UserFactory()
        personal = _add(db, user.id, "withings-personal", created_at=_EARLIER)
        provisioned = _add(db, user.id, "withings-provisioned", created_at=_LATER)

        assert repo.disconnect_connection(db, provisioned) == 1

        db.refresh(personal)
        db.refresh(provisioned)
        assert provisioned.status == ConnectionStatus.REVOKED
        assert provisioned.access_token is None
        assert personal.status == ConnectionStatus.ACTIVE, "removing one device revoked the member's own account"
        assert personal.access_token == "token"

    def test_the_wide_disconnect_still_revokes_everything(self, db: Session) -> None:
        # "Disconnect Withings" in its widest sense is still a coherent request, and is what
        # every caller that passes no connection_id gets.
        user = UserFactory()
        _add(db, user.id, "withings-personal", created_at=_EARLIER)
        _add(db, user.id, "withings-provisioned", created_at=_LATER)

        assert repo.disconnect(db, user.id, "withings") == 2

        statuses = {c.status for c in db.query(UserConnection).filter(UserConnection.user_id == user.id).all()}
        assert statuses == {ConnectionStatus.REVOKED}
