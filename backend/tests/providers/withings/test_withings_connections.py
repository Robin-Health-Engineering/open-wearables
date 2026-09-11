"""Telling a member's own Withings account from one we created to ship them a device.

There is no ``kind`` column: ``user_connection`` is upstream's table, and every column this
fork adds to it is a permanent rebase surface for a distinction the other twelve providers will
never make. The discriminator already existed in the schema instead — provisioning always
writes a ``withings_sdk_account`` row, consumer OAuth never does.

That inference is load-bearing rather than cosmetic. Disconnect branches on it to decide
whether to tell Withings to end a cellular programme, so getting it backwards would either
leave a plan billing us or ask Withings to end one that never existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.connections import (
    device_connections,
    is_device_connection,
    member_linked_connection,
)
from tests.factories import UserFactory

_EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _connection(
    db: Session,
    user_id: UUID,
    provider_user_id: str,
    *,
    provisioned: bool = False,
    status: ConnectionStatus = ConnectionStatus.ACTIVE,
    created_at: datetime = _EARLIER,
    provider: str = "withings",
) -> UserConnection:
    connection = UserConnection(
        id=uuid4(),
        user_id=user_id,
        provider=provider,
        provider_user_id=provider_user_id,
        access_token="token",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(connection)
    db.flush()
    if provisioned:
        db.add(
            WithingsSdkAccount(
                id=uuid4(),
                user_connection_id=connection.id,
                external_id=f"external-{provider_user_id}",
                csrf_token="csrf",
                updated_at=created_at,
            )
        )
        db.flush()
    return connection


class TestMemberLinkedConnection:
    def test_finds_the_connection_with_no_sdk_account_row(self, db: Session) -> None:
        user = UserFactory()
        personal = _connection(db, user.id, "withings-personal")
        _connection(db, user.id, "withings-provisioned", provisioned=True, created_at=_EARLIER + timedelta(days=1))

        found = member_linked_connection(db, user.id)

        assert found is not None
        assert found.id == personal.id

    def test_is_none_when_every_account_is_one_we_provisioned(self, db: Session) -> None:
        # A member who never linked an account of their own — the ordinary cellular-only case.
        user = UserFactory()
        _connection(db, user.id, "withings-provisioned", provisioned=True)

        assert member_linked_connection(db, user.id) is None

    def test_ignores_a_revoked_connection(self, db: Session) -> None:
        user = UserFactory()
        _connection(db, user.id, "withings-personal", status=ConnectionStatus.REVOKED)

        assert member_linked_connection(db, user.id) is None


class TestDeviceConnections:
    def test_returns_every_provisioned_account_oldest_first(self, db: Session) -> None:
        # A list, not an optional: Withings creates an account on every provisioning path and a
        # device cannot join one that exists, so a member accumulates one per cellular order.
        user = UserFactory()
        _connection(db, user.id, "withings-personal")
        _connection(db, user.id, "withings-order-1", provisioned=True, created_at=_EARLIER + timedelta(days=1))
        _connection(db, user.id, "withings-order-2", provisioned=True, created_at=_EARLIER + timedelta(days=2))

        found = device_connections(db, user.id)

        assert [c.provider_user_id for c in found] == ["withings-order-1", "withings-order-2"]

    def test_is_empty_for_a_member_who_only_linked_their_own(self, db: Session) -> None:
        user = UserFactory()
        _connection(db, user.id, "withings-personal")

        assert device_connections(db, user.id) == []

    def test_one_members_accounts_do_not_reach_another(self, db: Session) -> None:
        first = UserFactory()
        second = UserFactory()
        _connection(db, first.id, "withings-order-1", provisioned=True)

        assert device_connections(db, second.id) == []

    def test_another_providers_connection_is_never_a_withings_device(self, db: Session) -> None:
        user = UserFactory()
        _connection(db, user.id, "garmin-1", provider="garmin")

        assert device_connections(db, user.id) == []
        assert member_linked_connection(db, user.id) is None


class TestIsDeviceConnection:
    def test_true_for_a_provisioned_connection(self, db: Session) -> None:
        user = UserFactory()
        provisioned = _connection(db, user.id, "withings-provisioned", provisioned=True)

        assert is_device_connection(db, provisioned.id) is True

    def test_false_for_the_members_own_account(self, db: Session) -> None:
        # If this were true, disconnecting a personal account would ask Withings to end a
        # cellular programme that does not exist.
        user = UserFactory()
        personal = _connection(db, user.id, "withings-personal")

        assert is_device_connection(db, personal.id) is False

    def test_answers_for_a_revoked_connection_too(self, db: Session) -> None:
        # Unlike the two helpers above, which list what a member currently has. A disconnect
        # resolves the connection first and asks this second, by which point it may already
        # have been revoked — and a revoked device connection still has a plan to end.
        user = UserFactory()
        provisioned = _connection(
            db, user.id, "withings-provisioned", provisioned=True, status=ConnectionStatus.REVOKED
        )

        assert is_device_connection(db, provisioned.id) is True
