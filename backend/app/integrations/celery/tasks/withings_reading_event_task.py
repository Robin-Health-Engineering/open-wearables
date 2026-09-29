"""Deliver ``withings.reading.created`` to Robin, with bounded retries.

Its own task, not a step of ``process_webhook_push``: a Robin outage must not re-run the Withings
fetch, and a Withings failure must not re-send events. Enqueued after the samples commit.
"""

from logging import getLogger
from typing import Any

from celery import Task, shared_task
from celery.exceptions import MaxRetriesExceededError

from app.services.providers.withings.reading_events import DELIVER_TASK, post_event
from app.utils.sentry_helpers import log_and_capture_error

logger = getLogger(__name__)

# 3 attempts in total (spec C1): the first run plus two retries.
MAX_RETRIES = 2
_BACKOFF_BASE_SECONDS = 10


@shared_task(name=DELIVER_TASK, bind=True, acks_late=True, max_retries=MAX_RETRIES)
def deliver_withings_reading_event(self: Task, payload: dict[str, Any]) -> dict[str, Any]:
    outcome = post_event(payload)
    if outcome != "retry":
        return {"outcome": outcome}
    attempt = self.request.retries
    if attempt >= self.max_retries:
        # Nothing is lost but the push: the samples were committed long before this ran.
        error = MaxRetriesExceededError(f"Robin reading event undelivered after {attempt + 1} attempts")
        log_and_capture_error(
            error,
            logger,
            "Robin reading event delivery gave up",
            extra={"provider": "withings", "grpid": payload.get("grpid"), "attempts": attempt + 1},
        )
        raise error
    # 10s, 20s: network errors and Robin 5xx only.
    raise self.retry(countdown=_BACKOFF_BASE_SECONDS * 2**attempt)
