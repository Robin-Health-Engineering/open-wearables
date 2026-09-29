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
