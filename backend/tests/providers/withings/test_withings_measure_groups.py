"""withings_measure_group: one row per Withings measurement group per connection."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import UserConnection, WithingsMeasureGroupRecord
from tests.factories import UserConnectionFactory, UserFactory


def _row(connection: UserConnection, grpid: str = "123", device_id: str | None = "dev-1") -> WithingsMeasureGroupRecord:
    return WithingsMeasureGroupRecord(
        id=uuid4(),
        user_id=connection.user_id,
        user_connection_id=connection.id,
        grpid=grpid,
        device_id=device_id,
        model="Body Pro 2",
        attrib=0,
        measured_at=datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc),
    )


def test_a_group_is_unique_per_connection(db: Session) -> None:
    connection = UserConnectionFactory(user=UserFactory(), provider="withings")
    db.add(_row(connection))
    db.flush()
    db.add(_row(connection))
    with pytest.raises(IntegrityError):
        db.flush()
