"""What ``provision_cellular_order`` leaves behind, and which half goes where.

The bot's finding on #8 was right: the provisioning flow was the new core logic and the only
tests were on ``create_user_order`` and the schemas. What was untested is exactly what a reader
would most want pinned — that it CREATES rather than replaces, that the account and the orders
come back separated, and that nothing about an order is written to Postgres.

Real session, because every claim here is about rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.providers.withings.dropshipment import (
    DropshipAddress,
    DropshipOrder,
    DropshipOrderResult,
    DropshipProduct,
    DropshipUserOrder,
)
from app.services.providers.withings.dropshipment import WithingsDropshipmentError
from app.services.providers.withings.sdk_provisioning import provision_cellular_order
from app.services.providers.withings.sdk_users import SdkTokens
from tests.factories import UserConnectionFactory, UserFactory

_EXTERNAL_ID = "profile-1#order-1"

_PROFILE: dict[str, Any] = {
    "email": "member@example.com",
    "shortname": "FRA",
    "birthdate": 643248000,
    "gender": 0,
    "weight_kg": 75.4,
    "height_m": 1.78,
    "preflang": "it_IT",
    "timezone_name": "Europe/Rome",
    "mailingpref": 0,
    "unit_pref": {"weight": 1, "height": 6},
}


def _order() -> DropshipOrder:
    return DropshipOrder(
        customer_ref_id="order-1",
        address=DropshipAddress(
            name="Francesco Rossi",
            email="member@example.com",
            address1="Via Roma 1",
            city="Milano",
            zip="20121",
            country="IT",
        ),
        products=[DropshipProduct(quantity=1, ean="3700546705526")],
    )


def _tokens(userid: str) -> SdkTokens:
    return SdkTokens(
        userid=userid,
        access_token=f"access-{userid}",
        refresh_token=f"refresh-{userid}",
        csrf_token=f"csrf-{userid}",
        expires_in=10800,
        scope="user.metrics",
    )


def _provision(
    db: Session,
    user_id: UUID,
    *,
    withings_userid: str = "withings-cellular",
    external_id: str = _EXTERNAL_ID,
    orders: list[DropshipOrderResult] | None = None,
) -> Any:
    """Run cellular provisioning with both Withings calls stubbed at their import site."""
    user_order = DropshipUserOrder(
        code="auth-code",
        external_id=external_id,
        orders=orders if orders is not None else [DropshipOrderResult(orderid="WO-1", status="PENDING")],
    )
    with (
        patch(
            "app.services.providers.withings.sdk_provisioning.create_user_order",
            return_value=user_order,
        ),
        patch(
            "app.services.providers.withings.sdk_provisioning.exchange_sdk_code",
            return_value=_tokens(withings_userid),
        ),
    ):
        return provision_cellular_order(
            db,
            user_id=user_id,
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://api.example.com/api/v1/oauth/withings/callback",
            external_id=external_id,
            orders=[_order()],
            **_PROFILE,
        )


class TestWhatItPersists:
    def test_creates_a_connection_and_leaves_the_members_own_alone(self, db: Session) -> None:
        # The regression the shared helper's comment describes, exercised through the cellular
        # path rather than only the SDK one.
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="withings",
            provider_user_id="withings-personal",
            access_token="access-personal",
        )

        _provision(db, user.id)

        connections = db.query(UserConnection).filter(UserConnection.user_id == user.id).all()
        assert {c.provider_user_id for c in connections} == {"withings-personal", "withings-cellular"}
        personal = next(c for c in connections if c.provider_user_id == "withings-personal")
        assert personal.access_token == "access-personal"

    def test_the_sdk_account_row_stores_the_external_id_we_sent(self, db: Session) -> None:
        user = UserFactory()

        result = _provision(db, user.id)

        stored = db.query(WithingsSdkAccount).one()
        assert stored.external_id == _EXTERNAL_ID
        assert result.account.id == stored.id

    def test_no_order_is_written_to_postgres(self, db: Session) -> None:
        # The storage boundary, asserted rather than asserted-in-prose: order state is
        # robin-backend's, and nothing in this fork's schema should be able to hold it.
        user = UserFactory()

        _provision(db, user.id)

        columns = {c.name for c in WithingsSdkAccount.__table__.columns} | {
            c.name for c in UserConnection.__table__.columns
        }
        assert not {"orderid", "order_id", "shipment_status", "tracking_url"} & columns


class TestTheTwoHalves:
    def test_returns_the_account_and_the_orders_separately(self, db: Session) -> None:
        user = UserFactory()

        result = _provision(
            db,
            user.id,
            orders=[
                DropshipOrderResult(orderid="WO-1", status="PENDING"),
                DropshipOrderResult(orderid="WO-2", status="PENDING"),
            ],
        )

        assert result.account.external_id == _EXTERNAL_ID
        assert [o.orderid for o in result.orders] == ["WO-1", "WO-2"]

    def test_a_members_second_order_reuses_the_first_orders_account(self, db: Session) -> None:
        # Withings reuse the account createuserorder made, so a second order comes back with the
        # same external_id AND the same Withings userid. robin-backend sends a stable external_id
        # for exactly that reason, and this pins the consequence: one account, one connection,
        # however many devices a member buys.
        #
        # It used to assert the opposite — two of each, under a per-order external_id. That was
        # the honest reading of the docs and is not what Withings do.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-ours", external_id="profile-1")

        _provision(db, user.id, withings_userid="withings-ours", external_id="profile-1")

        assert db.query(WithingsSdkAccount).count() == 1
        assert db.query(UserConnection).filter(UserConnection.user_id == user.id).count() == 1


class TestFailureIsDiscriminable:
    def test_a_failed_store_raises_the_dropshipment_error_not_the_sdk_one(self, db: Session) -> None:
        # A cellular store failure strands an account AND a placed order; the SDK one strands
        # only an account. A caller catching WithingsDropshipmentError must see the failure it
        # most needs to hear about, which is why the shared helper takes the error type.
        #
        # Reached through the real collision now rather than a patched-out repository call: the
        # store no longer goes through `UserConnectionRepository.create`, so the old
        # `return_value=None` patch had nothing left to bite (open-wearables#12). Provisioning the
        # same external_id twice is the genuine article and exercises the same branch.
        user = UserFactory()
        _provision(db, user.id, withings_userid="withings-first")

        with pytest.raises(WithingsDropshipmentError, match="could not be stored") as exc:
            _provision(db, user.id, withings_userid="withings-second")

        assert exc.value.already_exists is True
