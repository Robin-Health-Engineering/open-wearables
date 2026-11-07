"""
Tests for DataSourceRepository.

Regression coverage for the ``data_source.source`` column width: Apple
HealthKit tags on-device data with a source bundle identifier of the form
``com.apple.health.<UUID>`` (53 chars). This overflowed the previous
``VARCHAR(50)`` and aborted the entire SDK import batch with
``StringDataRightTruncation``, so no Apple sleep/records were ever saved.
The column is now ``VARCHAR(100)``.
"""

from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository, _identity_sort_key
from app.schemas.enums import ProviderName
from tests.factories import UserFactory

# Realistic Apple HealthKit source bundle id: "com.apple.health." + a UUID.
APPLE_HEALTH_SOURCE = "com.apple.health.ED447642-08FD-4E45-AF20-633C02C83170"


class TestDataSourceRepository:
    """Test suite for DataSourceRepository."""

    def test_apple_health_source_bundle_id_persists(self, db: Session) -> None:
        """A 53-char Apple source bundle id imports without truncation.

        Exercises ``ensure_data_source`` (the SDK import path that failed) and
        asserts the full identifier round-trips — this would raise
        ``StringDataRightTruncation`` against the old ``VARCHAR(50)`` column.
        """
        # Arrange: a source longer than the old 50-char limit.
        assert len(APPLE_HEALTH_SOURCE) > 50
        user = UserFactory()
        repo = DataSourceRepository(DataSource)

        # Act
        created = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        db.commit()
        db.expire_all()

        # Assert: stored intact, retrievable by its full identity.
        assert created.source == APPLE_HEALTH_SOURCE
        stored = repo.get_by_identity(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        assert stored is not None
        assert stored.source == APPLE_HEALTH_SOURCE
        assert len(stored.source) == len(APPLE_HEALTH_SOURCE)


class TestBatchEnsureDataSourcesDeadlockSafety:
    """Regression coverage for OW-BACKEND-V (deadlock between concurrent SDK uploads)."""

    def test_identity_sort_key_is_total_and_none_safe(self) -> None:
        user_id = uuid4()
        identities = [
            (user_id, "22101316G", "com.google.android.apps.fitness"),
            (user_id, None, "com.google.android.apps.fitness"),
            (user_id, "", "com.google.android.apps.fitness"),
            (user_id, "22101316G", "com.android.healthconnect.phone"),
            (user_id, None, None),
        ]
        forward = sorted(identities, key=_identity_sort_key)
        backward = sorted(reversed(identities), key=_identity_sort_key)

        # Same order regardless of input order, and None never compares against str.
        assert forward == backward
        assert len({_identity_sort_key(i) for i in identities}) == len(identities)

    def test_batch_ensure_inserts_in_deterministic_order(self, db: Session) -> None:
        user = UserFactory()
        repo = DataSourceRepository(DataSource)
        identities = {
            (user.id, "22101316G", "com.android.healthconnect.phone"),
            (user.id, None, "com.google.android.apps.fitness"),
            (user.id, "22101316G", "com.google.android.apps.fitness"),
        }
        executed: list[object] = []
        original_execute = db.execute

        def spy(statement: object, *args: object, **kwargs: object) -> object:
            executed.append(statement)
            return original_execute(statement, *args, **kwargs)

        with patch.object(db, "execute", side_effect=spy):
            result = repo.batch_ensure_data_sources(db, ProviderName.GOOGLE, None, identities)

        assert set(result) == identities
        insert_stmt = executed[-1]
        params = insert_stmt.compile().params
        inserted = [
            (params[f"user_id_m{i}"], params[f"device_model_m{i}"], params[f"source_m{i}"])
            for i in range(len(identities))
        ]
        assert inserted == sorted(identities, key=_identity_sort_key)

    def test_lock_user_import_is_reentrant_within_transaction(self, db: Session) -> None:
        user = UserFactory()
        repo = DataSourceRepository(DataSource)

        repo.lock_user_import(db, user.id)
        # Same transaction can take it again (e.g. a later batch in the same import) without blocking.
        repo.lock_user_import(db, user.id)

        held = db.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        ).scalar_one()
        assert held >= 1
