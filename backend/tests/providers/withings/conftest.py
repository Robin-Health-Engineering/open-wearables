"""Shared Withings fixtures.

``make_provisioned_connection`` builds the account WE created to ship a member a device: an
active Withings ``user_connection`` plus its ``withings_sdk_account`` row, which is what
``connections.device_connections`` keys on. A connection made without it is the member's own,
self-linked account.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import User, UserConnection
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from tests.factories import UserConnectionFactory, UserFactory

ProvisionedConnectionMaker = Callable[..., tuple[User, UserConnection]]

_PROVISIONED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def make_provisioned_connection(db: Session) -> ProvisionedConnectionMaker:
    """Call as ``make_provisioned_connection(user=None)`` -> ``(user, connection)``.

    A new user is created when none is given. Each call adds a fresh connection and sdk row.
    """

    def _make(user: User | None = None) -> tuple[User, UserConnection]:
        user = user or UserFactory()
        connection = UserConnectionFactory(user=user, provider="withings")
        db.add(
            WithingsSdkAccount(
                id=uuid4(),
                user_connection_id=connection.id,
                external_id=f"cp-{uuid4().hex}",
                csrf_token="csrf",
                updated_at=_PROVISIONED_AT,
            )
        )
        db.flush()
        return user, connection

    return _make


def self_linked_connection(
    db: Session, *, user: User | None = None, external_user_id: str | None = None
) -> tuple[User, UserConnection]:
    """The member's OWN Withings account (consumer OAuth): an active connection with no withings_sdk_account row.

    ``external_user_id`` is the Robin CustomerProfile id ``ensureOwUser`` writes on the OW user; it is
    what a self-linked connection's reading event carries (contract C3). Ignored when ``user`` is given.
    """
    user = user or UserFactory(external_user_id=external_user_id)
    connection = UserConnectionFactory(user=user, provider="withings", provider_user_id=f"own-{uuid4().hex[:8]}")
    db.flush()
    return user, connection


def add_device(
    db: Session,
    connection: UserConnection,
    device_id: str,
    *,
    hash_device_id: str | None = None,
    order_ref: str | None = None,
) -> WithingsDevice:
    """A withings_device row on ``connection``. ``order_ref`` set makes it a device Robin SOLD (spec O2)."""
    device = WithingsDevice(
        id=uuid4(),
        user_connection_id=connection.id,
        device_id=device_id,
        hash_device_id=hash_device_id,
        order_ref=order_ref,
        updated_at=_PROVISIONED_AT,
    )
    db.add(device)
    db.flush()
    return device
