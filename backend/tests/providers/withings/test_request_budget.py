from unittest.mock import patch

import pytest

from app.config import settings
from app.services.providers.withings.request_budget import WithingsRequestBudgetExceeded, acquire_request_slot


def test_budget_globally_spaces_requests() -> None:
    with (
        patch.object(settings, "withings_api_requests_per_minute", 120),
        patch("app.services.providers.withings.request_budget.time.sleep") as sleep,
    ):
        acquire_request_slot()
        acquire_request_slot()

    sleep.assert_called_once()
    assert 0 < sleep.call_args.args[0] <= 0.5


def test_budget_admits_an_overridden_wait_that_covers_the_queue() -> None:
    with (
        patch.object(settings, "withings_api_requests_per_minute", 120),
        patch("app.services.providers.withings.request_budget.time.sleep") as sleep,
    ):
        acquire_request_slot()
        with pytest.raises(WithingsRequestBudgetExceeded):
            acquire_request_slot(max_wait_seconds=0)
        acquire_request_slot(max_wait_seconds=5)

    assert 0 < sleep.call_args.args[0] <= 0.5


def test_budget_rejects_queue_beyond_bounded_wait() -> None:
    with (
        patch.object(settings, "withings_api_requests_per_minute", 120),
        patch("app.services.providers.withings.request_budget.time.sleep"),
    ):
        for _ in range(11):
            acquire_request_slot()
        with pytest.raises(WithingsRequestBudgetExceeded):
            acquire_request_slot()
