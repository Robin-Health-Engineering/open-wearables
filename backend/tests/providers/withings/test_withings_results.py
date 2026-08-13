import json
from uuid import uuid4

import pytest

from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus
from app.services.providers.withings.results import IngestionResult, WithingsUserWebhookResult


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (IngestionResult(0), SyncStatus.SKIPPED),
        (IngestionResult(2), SyncStatus.SUCCESS),
        (IngestionResult(2, failed=1), SyncStatus.PARTIAL),
        (IngestionResult(0, failed=2), SyncStatus.FAILED),
    ],
)
def test_ingestion_result_is_int_compatible_and_serializable(result: IngestionResult, status: SyncStatus) -> None:
    assert isinstance(result, int)
    assert int(result) == result.processed
    assert result.status == status
    assert json.loads(json.dumps(result.to_dict())) == result.to_dict()


def test_ingestion_result_serializes_optional_write_counts() -> None:
    result = IngestionResult(3, write_counts=WriteCounts(1, 2), skipped=4, failed=1)

    assert result.to_dict() == {
        "status": "partial",
        "items_processed": 3,
        "inserted": 1,
        "updated": 2,
        "skipped": 4,
        "failed": 1,
    }


def test_ingestion_result_exposes_zero_write_counts_when_the_split_is_unknown() -> None:
    result = IngestionResult(3)
    pull_inserted = 0
    pull_updated = 0

    pull_inserted += getattr(result, "inserted", 0)
    pull_updated += getattr(result, "updated", 0)

    assert result.write_counts is None
    assert pull_inserted == 0
    assert pull_updated == 0
    assert "inserted" not in result.to_dict()
    assert "updated" not in result.to_dict()


def test_user_webhook_result_combines_and_serializes_components() -> None:
    user_id = uuid4()
    result = WithingsUserWebhookResult(
        user_id=user_id,
        domain="activity_workouts",
        components={
            "activity": IngestionResult(2, write_counts=WriteCounts(1, 1)),
            "workouts": IngestionResult(1, skipped=2, failed=1),
        },
    )

    assert result.combined == 3
    assert result.status == SyncStatus.PARTIAL
    assert result.items_processed == 3
    assert result.skipped == 2
    assert result.failed == 1
    assert result.to_dict() == {
        "user_id": str(user_id),
        "status": "partial",
        "items_processed": 3,
        "skipped": 2,
        "failed": 1,
        "components": {
            "activity": {
                "status": "success",
                "items_processed": 2,
                "skipped": 0,
                "failed": 0,
                "inserted": 1,
                "updated": 1,
            },
            "workouts": {
                "status": "partial",
                "items_processed": 1,
                "skipped": 2,
                "failed": 1,
            },
        },
    }
    assert json.loads(json.dumps(result.to_dict())) == result.to_dict()
    assert result.metadata() == {
        "domain": "activity_workouts",
        "skipped": 2,
        "failed": 1,
        "components": result.to_dict()["components"],
    }
