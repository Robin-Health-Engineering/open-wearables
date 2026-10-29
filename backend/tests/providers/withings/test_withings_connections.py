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

import pytest
from sqlalchemy.orm import Session

from app.models import User, UserConnection, WithingsMeasureGroupRecord
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.connections import (
    RobinDeviceScope,
    device_connections,
    is_device_connection,
    member_linked_connection,
    robin_device_scope,
    robin_device_scopes,
    robin_external_user_id,
    robin_groups_clause,
)
from tests.factories import UserFactory
from tests.providers.withings.conftest import ProvisionedConnectionMaker, add_device, self_linked_connection

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
        # A list, not an optional. A member holds at most one provisioned account today, but the
        # shape is what stops a caller writing `the` provisioned connection — historic members can
        # hold more than one, and the personal-vs-provisioned split is the distinction that
        # matters. The two rows below are exactly that case.
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


def _group_row(
    db: Session, connection: UserConnection, grpid: str, device_id: str | None, hash_device_id: str | None
) -> None:
    db.add(
        WithingsMeasureGroupRecord(
            id=uuid4(),
            user_id=connection.user_id,
            user_connection_id=connection.id,
            grpid=grpid,
            device_id=device_id,
            hash_device_id=hash_device_id,
            model="Body Comp",
            attrib=0,
            measured_at=_EARLIER,
        )
    )
    db.flush()


class TestRobinDeviceScope:
    """Which weigh-ins are from a device Robin sold (spec O2): every device on an account we provisioned,
    only the sold device (``order_ref`` set) on the member's own account."""

    def test_a_provisioned_connection_covers_every_device(
        self, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
    ) -> None:
        _, connection = make_provisioned_connection()

        scope = robin_device_scope(db, connection.id)

        assert scope == RobinDeviceScope(connection_id=connection.id, provisioned=True)
        assert scope.covers(None, "any-device")

    def test_a_self_linked_connection_without_a_sold_device_is_out_of_scope(self, db: Session) -> None:
        _, own = self_linked_connection(db)
        add_device(db, own, "family-scale", hash_device_id="family-hash")

        assert robin_device_scope(db, own.id) is None

    def test_a_self_linked_connection_covers_only_its_sold_device(self, db: Session) -> None:
        _, own = self_linked_connection(db)
        add_device(db, own, "bodycomp-1", hash_device_id="bc-hash", order_ref="ORDER-1")
        add_device(db, own, "family-scale", hash_device_id="family-hash")

        scope = robin_device_scope(db, own.id)

        assert scope == RobinDeviceScope(
            connection_id=own.id, provisioned=False, sold_keys=frozenset({"bodycomp-1", "bc-hash"})
        )
        assert scope.covers("bc-hash", "15542329")  # the hash joins a group whose deviceid Getdevice never lists
        assert scope.covers(None, "bodycomp-1")  # a hashless group joins by its deviceid
        assert not scope.covers("family-hash", "family-scale")
        assert not scope.covers(None, None)

    def test_a_members_scopes_are_the_provisioned_account_and_the_sold_device_on_their_own(
        self, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
    ) -> None:
        user, provisioned = make_provisioned_connection()
        _, own = self_linked_connection(db, user=user)
        add_device(db, own, "bodycomp-1", hash_device_id="bc-hash", order_ref="ORDER-1")
        _, other_own = self_linked_connection(db, user=user)  # a second personal account, nothing sold on it
        add_device(db, other_own, "family-scale", hash_device_id="family-hash")

        scopes = robin_device_scopes(db, user.id)

        assert {(s.connection_id, s.provisioned) for s in scopes} == {(provisioned.id, True), (own.id, False)}

    def test_a_revoked_self_linked_connection_counts_only_in_any_state(self, db: Session) -> None:
        user, own = self_linked_connection(db)
        add_device(db, own, "bodycomp-1", hash_device_id="bc-hash", order_ref="ORDER-1")
        own.status = ConnectionStatus.REVOKED
        db.flush()

        assert robin_device_scopes(db, user.id) == []
        assert [s.connection_id for s in robin_device_scopes(db, user.id, any_state=True)] == [own.id]

    def test_the_groups_clause_selects_the_provisioned_account_and_only_the_sold_device_on_their_own(
        self, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
    ) -> None:
        user, provisioned = make_provisioned_connection()
        _, own = self_linked_connection(db, user=user)
        add_device(db, own, "bodycomp-1", hash_device_id="bc-hash", order_ref="ORDER-1")
        _group_row(db, provisioned, "p1", "cell-1", None)
        _group_row(db, own, "o1", "15542329", "bc-hash")  # by hash
        _group_row(db, own, "o2", "bodycomp-1", None)  # by deviceid
        _group_row(db, own, "o3", "family-scale", "family-hash")  # the family's scale

        clause = robin_groups_clause(robin_device_scopes(db, user.id))
        assert clause is not None
        grpids = {row[0] for row in db.query(WithingsMeasureGroupRecord.grpid).filter(clause)}

        assert grpids == {"p1", "o1", "o2"}

    def test_no_scope_is_no_clause(self) -> None:
        assert robin_groups_clause([]) is None


class TestRobinExternalUserId:
    """Who a reading event is for (contract C3)."""

    def test_a_provisioned_connection_is_its_sdk_accounts_external_id(
        self, db: Session, make_provisioned_connection: ProvisionedConnectionMaker
    ) -> None:
        _, connection = make_provisioned_connection()
        expected = db.query(WithingsSdkAccount.external_id).filter_by(user_connection_id=connection.id).scalar()

        assert robin_external_user_id(db, connection) == expected

    def test_a_self_linked_connection_is_the_members_robin_profile_id(self, db: Session) -> None:
        _, own = self_linked_connection(db, external_user_id="cp-self-1")

        assert robin_external_user_id(db, own) == "cp-self-1"

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_self_linked_connection_without_one_is_none(self, db: Session, value: str | None) -> None:
        user, own = self_linked_connection(db)
        db.get(User, user.id).external_user_id = value
        db.flush()

        assert robin_external_user_id(db, own) is None
