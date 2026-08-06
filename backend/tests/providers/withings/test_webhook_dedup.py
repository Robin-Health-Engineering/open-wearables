from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.services.providers.withings.webhook_dedup import claim_fetch

_START = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
_END = _START + timedelta(seconds=1)


def _claim(
    user: str,
    *,
    domain: str = "measures",
    start: datetime = _START,
    end: datetime = _END,
    notification_id: str | None = None,
) -> AbstractContextManager[bool]:
    return claim_fetch(
        withings_user_id=user,
        domain=domain,
        start=start,
        end=end,
        notification_id=notification_id or uuid4().hex[:8],
    )


def test_only_the_first_notification_of_a_burst_claims_the_fetch() -> None:
    user = uuid4().hex

    with _claim(user) as claimed:
        assert claimed is True
    for _ in range(2):
        with _claim(user) as claimed:
            assert claimed is False


def test_a_failed_fetch_releases_the_claim() -> None:
    user = uuid4().hex

    def failing_fetch() -> None:
        with _claim(user) as claimed:
            assert claimed is True
            raise RuntimeError("fetch failed")

    with pytest.raises(RuntimeError):
        failing_fetch()

    with _claim(user) as claimed:
        assert claimed is True


def test_a_redelivered_notification_reclaims_the_claim_its_killed_run_left_behind() -> None:
    user = uuid4().hex
    notification = uuid4().hex[:8]

    with _claim(user, notification_id=notification) as claimed:
        assert claimed is True  # killed here: the claim outlives the process

    with _claim(user, notification_id=notification) as claimed:
        assert claimed is True


def test_a_reclaimed_window_still_shuts_out_the_siblings() -> None:
    user = uuid4().hex
    notification = uuid4().hex[:8]

    with _claim(user, notification_id=notification) as claimed:
        assert claimed is True
    with _claim(user, notification_id=notification) as claimed:
        assert claimed is True

    with _claim(user) as claimed:
        assert claimed is False


def test_a_later_measurement_is_a_separate_fetch() -> None:
    user = uuid4().hex

    with _claim(user) as claimed:
        assert claimed is True

    later = _START + timedelta(hours=1)
    with _claim(user, start=later, end=later + timedelta(seconds=1)) as claimed:
        assert claimed is True


def test_each_domain_claims_independently() -> None:
    user = uuid4().hex

    with _claim(user, domain="measures") as claimed:
        assert claimed is True
    with _claim(user, domain="sleep") as claimed:
        assert claimed is True


def test_each_withings_account_claims_independently() -> None:
    with _claim(uuid4().hex) as claimed:
        assert claimed is True
    with _claim(uuid4().hex) as claimed:
        assert claimed is True


def test_a_redis_outage_fetches_anyway() -> None:
    with (
        patch(
            "app.services.providers.withings.webhook_dedup.get_redis_client",
            side_effect=RedisError("redis is down"),
        ),
        _claim(uuid4().hex) as claimed,
    ):
        assert claimed is True
