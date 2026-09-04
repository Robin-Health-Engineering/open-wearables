"""Provisioning replaces a connection; these pin what "replace" has to mean.

``user_connection`` has a unique ``(user_id, provider)`` index, so an SDK-provisioned Withings
account cannot sit alongside a personally-linked one — provisioning overwrites the row. The
overwrite is the interesting path, because a row half-updated is worse than either end state:
the tokens belong to the new Withings account while ``provider_user_id``, which is how every
inbound Withings notification is routed back to a connection, still names the old one.

Uses the real session fixture rather than a mock, on purpose. The bug this file is written
against was a repository method quietly declining to update a field
(``update_connection_info`` only fills ``provider_user_id`` in when the row has none), and a
mock session asserts on the call, not on the row it left behind.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.services.providers.withings.sdk_provisioning import provision_sdk_account
from app.services.providers.withings.sdk_users import SdkTokens, SdkUser
from tests.factories import UserConnectionFactory, UserFactory

_EXTERNAL_ID = "robin-user-1"

_PROFILE = {
    "external_id": _EXTERNAL_ID,
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
        access_token="access-new",
        refresh_token="refresh-new",
        csrf_token="csrf-new",
        expires_in=10800,
        scope="user.metrics,user.activity",
    )


def _provision(db: Session, user_id: UUID, *, withings_userid: str) -> WithingsSdkAccount:
    """Run provisioning with both Withings calls stubbed at their import site."""
    with (
        patch(
            "app.services.providers.withings.sdk_provisioning.create_sdk_user",
            return_value=SdkUser(code="auth-code", external_id=_EXTERNAL_ID),
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
            **_PROFILE,
        )


class TestProvisionSdkAccount:
    def test_replacing_a_connection_updates_the_routing_key(self, db: Session) -> None:
        # The case the code's own log line anticipates: a personally-linked account whose
        # Withings userid differs from the one provisioning just created.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-old")

        _provision(db, user.id, withings_userid="withings-new")

        connection = db.query(UserConnection).filter(UserConnection.user_id == user.id).one()
        # Left stale, this row would route every inbound Withings notification for the NEW
        # account to nothing, while the tokens on it belong to that account.
        assert connection.provider_user_id == "withings-new"
        assert connection.access_token == "access-new"
        assert connection.refresh_token == "refresh-new"

    def test_replacing_a_connection_keeps_one_row_and_one_sdk_account(self, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-old")

        account = _provision(db, user.id, withings_userid="withings-new")

        connections = db.query(UserConnection).filter(UserConnection.user_id == user.id).all()
        assert len(connections) == 1, "the unique (user_id, provider) index leaves room for one"
        assert account.user_connection_id == connections[0].id
        assert account.csrf_token == "csrf-new"

    def test_re_provisioning_reuses_the_sdk_account_row(self, db: Session) -> None:
        # csrf_token is reissued on every token refresh, so this row is rewritten often. Its
        # identity must not churn with it.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-old")

        first = _provision(db, user.id, withings_userid="withings-new")
        first_id = first.id
        second = _provision(db, user.id, withings_userid="withings-newer")

        assert second.id == first_id
        assert db.query(WithingsSdkAccount).count() == 1

    def test_provisioning_a_member_with_no_connection_creates_one(self, db: Session) -> None:
        user = UserFactory()

        account = _provision(db, user.id, withings_userid="withings-new")

        connection = db.query(UserConnection).filter(UserConnection.user_id == user.id).one()
        assert connection.provider == "withings"
        assert connection.provider_user_id == "withings-new"
        assert account.external_id == _EXTERNAL_ID
