import json
from uuid import uuid4

import pytest

from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus
from app.services.providers.withings.results import WithingsUserWebhookResult, write_status, write_summary


@pytest.mark.parametrize(
    ("counts", "status"),
    [
        (WriteCounts.unsplit(0), SyncStatus.SKIPPED),
        (WriteCounts.unsplit(2), SyncStatus.SUCCESS),
        (WriteCounts.unsplit(2, failed=1), SyncStatus.PARTIAL),
        (WriteCounts.unsplit(0, failed=2), SyncStatus.FAILED),
    ],
)
def test_write_counts_are_int_compatible_and_serializable(counts: WriteCounts, status: SyncStatus) -> None:
    assert isinstance(counts, int)
    assert write_status(counts) == status
    assert json.loads(json.dumps(write_summary(counts))) == write_summary(counts)


def test_write_summary_includes_the_split_when_known() -> None:
    counts = WriteCounts(1, 2, skipped=4, failed=1)

    assert write_summary(counts) == {
        "status": "partial",
        "items_processed": 3,
        "inserted": 1,
        "updated": 2,
        "skipped": 4,
        "failed": 1,
    }


def test_write_summary_omits_the_split_when_unknown() -> None:
    counts = WriteCounts.unsplit(3)

    assert counts.split_known is False
    assert "inserted" not in write_summary(counts)
    assert "updated" not in write_summary(counts)


def test_write_counts_reject_negatives() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        WriteCounts.unsplit(1, failed=-1)


def test_combine_keeps_the_split_only_when_every_part_reports_one() -> None:
    split = WriteCounts.combine([WriteCounts(1, 1), WriteCounts(0, 2)])
    mixed = WriteCounts.combine([WriteCounts(1, 1), WriteCounts.unsplit(1)])

    assert (int(split), split.split_known, split.inserted, split.updated) == (4, True, 1, 3)
    assert (int(mixed), mixed.split_known) == (3, False)


def test_user_webhook_result_combines_and_serializes_components() -> None:
    user_id = uuid4()
    result = WithingsUserWebhookResult(
        user_id=user_id,
        domain="activity_workouts",
        components={
            "activity": WriteCounts(1, 1),
            "workouts": WriteCounts.unsplit(1, skipped=2, failed=1),
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
