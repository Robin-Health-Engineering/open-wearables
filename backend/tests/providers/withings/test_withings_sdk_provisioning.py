"""Provisioning ADDS a Withings account; these pin what "add" has to mean.

A member can hold TWO Withings connections. Withings creates an account on every provisioning
path they offer, and a cellular device cannot be activated onto an account the partner did not
create — so someone who has linked their own account and is then shipped a device holds both.

Two, not one per order: Withings confirmed (2026-09-15) that a member's later orders ship to the
account their first one created. So a repeat provisioning arrives with an external_id and a
Withings userid we already hold, and must REUSE that connection — the tests below say what reuse
is allowed to mean, and where it must still refuse.

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
from sqlalchemy.orm import Session

from app.integrations.celery.task_names import SYNC_PROVIDER_USER_SUBSCRIPTION_TASK
from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.connections import device_connections, member_linked_connection
from app.services.providers.withings.sdk_provisioning import provision_sdk_account
from app.services.providers.withings.sdk_users import SdkTokens, SdkUser, WithingsSdkUserError
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

    def test_a_repeat_order_reuses_the_connection_instead_of_adding_one(self, db: Session) -> None:
        # THE case the account model turns on. Withings reuse the account they created for a
        # member, so a second order comes back with the same userid under the same external_id —
        # and robin-backend now sends a stable external_id precisely because of that.
        #
        # Creating unconditionally here violates (user_id, provider, provider_user_id) AFTER
        # createuserorder has placed the order: the member's second device is paid for, shipped by
        # Withings, and unknown to us. That is what this prevents.
        user = UserFactory()
        UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-personal")

        first = _provision(db, user.id, withings_userid="withings-ours")
        second = _provision(db, user.id, withings_userid="withings-ours")

        connections = db.query(UserConnection).filter(UserConnection.user_id == user.id).all()
        assert len(connections) == 2, "the personal account and ours — never a third"
        assert len(device_connections(db, user.id)) == 1
        assert db.query(WithingsSdkAccount).count() == 1
        assert second.id == first.id, "the same SDK row, updated in place"

    def test_a_repeat_order_writes_the_new_tokens(self, db: Session) -> None:
        # Reuse is not a no-op. The code exchange just minted fresh tokens and the stored ones are
        # spent, so a reuse that kept the old row untouched would leave the connection holding
        # dead credentials — healthy-looking and unable to read the account.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-ours")

        with patch(
            "app.services.providers.withings.sdk_provisioning.create_sdk_user",
            return_value=SdkUser(code="auth-code", external_id=_EXTERNAL_ID),
        ):
            rotated = SdkTokens(
                userid="withings-ours",
                access_token="access-rotated",
                refresh_token="refresh-rotated",
                csrf_token="csrf-rotated",
                expires_in=10800,
                scope="user.metrics",
            )
            with patch(
                "app.services.providers.withings.sdk_provisioning.exchange_sdk_code",
                return_value=rotated,
            ):
                provision_sdk_account(
                    db,
                    user_id=user.id,
                    client_id="client-id",
                    client_secret="client-secret",
                    redirect_uri="https://api.example.com/api/v1/oauth/withings/callback",
                    external_id=_EXTERNAL_ID,
                    **_PROFILE,
                )

        connection = device_connections(db, user.id)[0]
        assert connection.access_token == "access-rotated"
        assert connection.refresh_token == "refresh-rotated"
        assert db.query(WithingsSdkAccount).one().csrf_token == "csrf-rotated"

    def test_a_repeat_order_reactivates_a_revoked_connection(self, db: Session) -> None:
        # A member can remove a device, or Withings can revoke upstream (`_revoke_local_connections`
        # revokes EVERY Withings connection they hold), and then order again — and Withings hand
        # back the SAME account, so reuse finds the revoked row.
        #
        # Writing live tokens onto a row that stays REVOKED is incoherent AND silent:
        # `active_withings_connections` filters on ACTIVE, so the new device is invisible to sync
        # and to `end_program` — the one with a SIM billing every month — while the route answers
        # 201 and the parcel ships. `ensure_sdk_connection` and `base_oauth` both reactivate on
        # their reuse paths; this one did not (Lucas, #13).
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-ours")
        UserConnectionRepository().disconnect(db, user.id, "withings")

        _provision(db, user.id, withings_userid="withings-ours")

        connection = db.query(UserConnection).filter(UserConnection.user_id == user.id).one()
        assert connection.status == ConnectionStatus.ACTIVE
        assert connection.access_token == "access-withings-ours"
        assert len(device_connections(db, user.id)) == 1

    def test_an_account_the_member_linked_themselves_is_not_adopted(self, db: Session) -> None:
        # `user_connection.py` keeps the three-column index as "the guard if Withings ever ADOPTS
        # an existing account instead of creating one — that comes back as the same
        # provider_user_id and fails loudly here". Reuse removes that loud failure unless this
        # refuses: the member's own connection matches on provider_user_id, carries no
        # withings_sdk_account row (connections.py: row absent means the member linked it), and
        # would otherwise have its personal tokens overwritten with partner-minted ones and an SDK
        # row attached — reclassifying their account as one we provisioned.
        user = UserFactory()
        UserConnectionFactory(
            user=user, provider="withings", provider_user_id="withings-theirs", access_token="their-token"
        )

        with pytest.raises(WithingsSdkUserError) as exc:
            _provision(db, user.id, withings_userid="withings-theirs")

        assert exc.value.already_exists is True
        linked = member_linked_connection(db, user.id)
        assert linked is not None
        assert linked.access_token == "their-token", "the member's own tokens survive"

    def test_a_repeat_order_does_not_re_announce_the_connection(self, db: Session) -> None:
        # robin-backend was told about this connection by the first order; re-firing presents an
        # existing connection as a new one to every consumer of that webhook.
        #
        # The FIRST call is asserted in the same body on purpose. An absence with no positive
        # control beside it also passes when the announcement stops happening at all — which is
        # the way this exact test usually rots.
        user = UserFactory()

        with patch("app.services.providers.withings.sdk_provisioning.on_connection_created") as emit:
            _provision(db, user.id, withings_userid="withings-ours")
            assert emit.call_count == 1, "the first provisioning must announce"

            _provision(db, user.id, withings_userid="withings-ours")
            assert emit.call_count == 1, "the repeat must not re-announce"

    def test_the_same_account_under_a_different_external_id_is_refused(self, db: Session) -> None:
        # The limit of reuse, and the reason it is an explicit check rather than a fallthrough.
        # external_id is the join robin-backend resolves an account by; one Withings account
        # answering to two names, or one name pointing at two accounts, breaks that link.
        # Rewriting the row silently would be strictly worse than refusing an order that can be
        # reconciled by hand, so this refuses.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-same", external_id=f"{_EXTERNAL_ID}-a")

        with pytest.raises(WithingsSdkUserError) as exc:
            _provision(db, user.id, withings_userid="withings-same", external_id=f"{_EXTERNAL_ID}-b")

        assert exc.value.already_exists is True

    def test_a_different_account_under_the_same_external_id_is_refused(self, db: Session) -> None:
        # The mirror image, caught by the external_id unique index rather than by the check above:
        # no existing connection matches this userid, so the insert goes ahead and collides.
        #
        # Under the old per-order external_id this was the ORDINARY second order and it was pinned
        # as a requirement on robin-backend to send {profileId}#{orderRef}. It is now a genuine
        # fault — Withings handing us a different account for a member we already provisioned —
        # and the same already_exists answer is the right one for a different reason.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-order-1")

        with pytest.raises(WithingsSdkUserError) as exc:
            _provision(db, user.id, withings_userid="withings-order-2")

        assert exc.value.already_exists is True


class TestSubscriptionScheduling:
    """Provisioning must ask for a Notify subscription, because nothing else will.

    ``WithingsNotifyService`` has been complete since the Notify work landed, but nothing on the
    PROVISIONING path enqueued it. The two existing enqueue sites are the OAuth callback and the
    ``register_subscriptions`` fan-out — and a provisioned account never goes through OAuth, while
    the fan-out has no ``beat_schedule`` entry and runs only when an operator hits the admin route
    or changes the live-sync mode.

    It failed silently in both directions. A missing subscription looks exactly like a member who
    has not stepped on the scale, so nothing surfaced it until someone went looking.
    """

    def test_provisioning_schedules_the_subscription_sync(self, db: Session) -> None:
        user = UserFactory()

        with patch("app.services.providers.withings.sdk_provisioning.celery_app") as celery:
            _provision(db, user.id, withings_userid="withings-ours")

        celery.send_task.assert_called_once()
        name, kwargs = celery.send_task.call_args[0][0], celery.send_task.call_args[1]
        assert name == SYNC_PROVIDER_USER_SUBSCRIPTION_TASK
        assert kwargs["args"] == ["withings", str(user.id)]
        assert kwargs["queue"] == "webhook_sync"

    def test_a_repeat_order_schedules_it_too(self, db: Session) -> None:
        # Reuse is the path most likely to skip this, and the one where skipping is least
        # visible: the connection already exists, so everything looks provisioned. But a
        # subscription can have been revoked, expired, or never made — the task reconciles rather
        # than blindly subscribing, so asking again is cheap and not asking is a silent gap.
        user = UserFactory()

        with patch("app.services.providers.withings.sdk_provisioning.celery_app") as celery:
            _provision(db, user.id, withings_userid="withings-ours")
            _provision(db, user.id, withings_userid="withings-ours")

        assert celery.send_task.call_count == 2

    def test_a_broker_failure_does_not_fail_the_provisioning(self, db: Session) -> None:
        # The account exists at Withings and the device is shipping. Reporting that as a failure
        # would send robin-backend down a path that strands a real order, to recover a
        # subscription the next fan-out can make anyway.
        user = UserFactory()

        with patch("app.services.providers.withings.sdk_provisioning.celery_app") as celery:
            celery.send_task.side_effect = RuntimeError("broker down")
            account = _provision(db, user.id, withings_userid="withings-ours")

        assert account.csrf_token == "csrf-withings-ours"
        assert len(device_connections(db, user.id)) == 1

    def test_it_is_scheduled_after_the_commit(self, db: Session) -> None:
        # Same rule as on_connection_created, and for the same reason: the worker resolves the
        # user's connections from the database, so a task dispatched before the commit can find
        # nothing and report a clean "no subscriptions to make".
        user = UserFactory()
        seen: list[int] = []

        with patch("app.services.providers.withings.sdk_provisioning.celery_app") as celery:
            celery.send_task.side_effect = lambda *a, **k: seen.append(
                db.query(WithingsSdkAccount).filter(WithingsSdkAccount.external_id == _EXTERNAL_ID).count()
            )
            _provision(db, user.id, withings_userid="withings-ours")

        assert seen == [1], "the row must be visible by the time the task is dispatched"
