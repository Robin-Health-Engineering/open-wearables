"""Provisioning ADDS a Withings account; these pin what "add" has to mean.

A member can hold several Withings connections at once. Withings creates an account on every
provisioning path they offer, and a cellular device cannot be activated onto an account the
partner did not create — so someone who has linked their own account and is then shipped a
device holds two, and another for every later order.

This used to overwrite instead, because ``user_connection``'s unique index was
``(user_id, provider)`` and there was nowhere to put a second row. The consequence was that
shipping a member a blood-pressure monitor silently stopped their own scale and watch from
syncing. The index now includes ``provider_user_id``, and the first two tests here are the
ones that would have passed under the old behaviour and must not now.

Uses the real session fixture rather than a mock, on purpose. Everything asserted here is a
claim about the ROWS left behind — how many, and which tokens are on which — and a mock
session asserts on the call instead.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.services.providers.withings.connections import device_connections, member_linked_connection
from app.services.providers.withings.sdk_provisioning import provision_sdk_account
from app.services.providers.withings.sdk_users import SdkTokens, SdkUser
from tests.factories import UserConnectionFactory, UserFactory

_EXTERNAL_ID = "robin-user-1"

_PROFILE = {
    "email": "member@example.com",
    "shortname": "FRA",
    "birthdate": 643248000,
    "gender": 0,
    "weight_kg": 75.4,
    "height_m": 1.78,
    "preflang": "it_IT",
    "timezone_name": "Europe/Rome",
    "mailingpref": 0,
}


def _tokens(userid: str) -> SdkTokens:
    return SdkTokens(
        userid=userid,
        access_token=f"access-{userid}",
        refresh_token=f"refresh-{userid}",
        csrf_token=f"csrf-{userid}",
        expires_in=10800,
        scope="user.metrics,user.activity",
    )


def _provision(
    db: Session,
    user_id: UUID,
    *,
    withings_userid: str,
    external_id: str = _EXTERNAL_ID,
) -> WithingsSdkAccount:
    """Run provisioning with both Withings calls stubbed at their import site."""
    with (
        patch(
            "app.services.providers.withings.sdk_provisioning.create_sdk_user",
            return_value=SdkUser(code="auth-code", external_id=external_id),
        ),
        patch(
            "app.services.providers.withings.sdk_provisioning.exchange_sdk_code",
            return_value=_tokens(withings_userid),
        ),
    ):
        return provision_sdk_account(
            db,
            user_id=user_id,
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://api.example.com/api/v1/oauth/withings/callback",
            external_id=external_id,
            **_PROFILE,
        )


class TestProvisionSdkAccount:
    def test_leaves_the_members_own_account_connected(self, db: Session) -> None:
        # THE regression. Under the old behaviour this row was overwritten in place, and the
        # member's own scale and watch stopped syncing the moment we shipped them a monitor.
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="withings",
            provider_user_id="withings-personal",
            access_token="access-personal",
            refresh_token="refresh-personal",
        )

        _provision(db, user.id, withings_userid="withings-provisioned")

        personal = (
            db.query(UserConnection)
            .filter(
                UserConnection.user_id == user.id,
                UserConnection.provider_user_id == "withings-personal",
            )
            .one()
        )
        assert personal.access_token == "access-personal", "the member's own tokens were overwritten"
        assert personal.refresh_token == "refresh-personal"

    def test_adds_a_second_connection_rather_than_replacing_the_first(self, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-personal")

        account = _provision(db, user.id, withings_userid="withings-provisioned")

        connections = db.query(UserConnection).filter(UserConnection.user_id == user.id).all()
        assert len(connections) == 2
        assert {c.provider_user_id for c in connections} == {"withings-personal", "withings-provisioned"}

        provisioned = next(c for c in connections if c.provider_user_id == "withings-provisioned")
        assert account.user_connection_id == provisioned.id, "the sdk_account hangs off the NEW connection"
        assert account.csrf_token == "csrf-withings-provisioned"

    def test_the_two_connections_are_told_apart_by_kind(self, db: Session) -> None:
        # There is no kind column: the discriminator is whether a withings_sdk_account row
        # exists. This is what disconnect branches on to decide whether to end a cellular
        # programme, so it has to hold on real rows and not just in principle.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-personal")

        _provision(db, user.id, withings_userid="withings-provisioned")

        linked = member_linked_connection(db, user.id)
        devices = device_connections(db, user.id)
        assert linked is not None
        assert linked.provider_user_id == "withings-personal"
        assert [c.provider_user_id for c in devices] == ["withings-provisioned"]

    def test_provisioning_a_member_with_no_connection_creates_one(self, db: Session) -> None:
        user = UserFactory()

        account = _provision(db, user.id, withings_userid="withings-provisioned")

        connection = db.query(UserConnection).filter(UserConnection.user_id == user.id).one()
        assert connection.provider == "withings"
        assert connection.provider_user_id == "withings-provisioned"
        assert account.external_id == _EXTERNAL_ID

    def test_a_second_order_needs_its_own_external_id(self, db: Session) -> None:
        # The constraint that decides how robin-backend must mint external_id. It is UNIQUE,
        # and today robin-backend sends the bare CustomerProfile id — one value per member for
        # life — so a member's SECOND provisioned account collides here, after Withings has
        # already created a real account on their side.
        #
        # Pinned rather than fixed in this repo: the fix is the {profileId}#{orderRef} format,
        # which is robin-backend's to send. This test is what makes that a requirement instead
        # of an intention.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-order-1")

        with pytest.raises(IntegrityError):
            _provision(db, user.id, withings_userid="withings-order-2")

    def test_a_second_order_with_its_own_external_id_adds_a_third_connection(self, db: Session) -> None:
        # And with a distinct external_id it works, which is what the format change buys: a
        # member's own account plus one per cellular order.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-personal")

        _provision(db, user.id, withings_userid="withings-order-1", external_id=f"{_EXTERNAL_ID}#order-1")
        _provision(db, user.id, withings_userid="withings-order-2", external_id=f"{_EXTERNAL_ID}#order-2")

        connections = db.query(UserConnection).filter(UserConnection.user_id == user.id).all()
        assert len(connections) == 3
        assert len(device_connections(db, user.id)) == 2
        assert db.query(WithingsSdkAccount).count() == 2

    def test_the_same_withings_account_twice_is_still_rejected(self, db: Session) -> None:
        # Relaxing the index did not make it meaningless. Two rows describing ONE Withings
        # account for one member is a bug, and it is also the guard if Withings ever adopts an
        # existing account instead of creating a new one.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-same", external_id=f"{_EXTERNAL_ID}#order-1")

        with pytest.raises(IntegrityError):
            _provision(db, user.id, withings_userid="withings-same", external_id=f"{_EXTERNAL_ID}#order-2")
