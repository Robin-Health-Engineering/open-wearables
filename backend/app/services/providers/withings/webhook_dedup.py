"""Deduplicate Withings notification fetches across Celery workers with Redis claims."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from redis.exceptions import RedisError

from app.integrations.redis_client import get_redis_client
from app.services.providers.withings.applis import Domain
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_KEY_PREFIX = "withings:webhook_dedup:v1"

# The key includes the exact data window, so the TTL only bounds suppression of
# a repeated notification for an edited measurement.
_CLAIM_TTL_SECONDS = 120


@contextmanager
def claim_fetch(
    *,
    withings_user_id: str,
    domain: Domain,
    start: datetime,
    end: datetime,
    notification_id: str,
) -> Iterator[bool]:
    """Claim a data window, allowing the owning notification to reclaim it on retry."""
    key = f"{_KEY_PREFIX}:{withings_user_id}:{domain}:{int(start.timestamp())}:{int(end.timestamp())}"
    try:
        # SET .. NX GET claims a free window and reports the owner of a taken one
        # in a single atomic step, so an expiry can never slip between the two.
        owner = get_redis_client().set(key, notification_id, nx=True, get=True, ex=_CLAIM_TTL_SECONDS)
    except RedisError as exc:
        # Deduplication is an optimization; a cache outage must not halt ingestion.
        log_structured(
            logger,
            "warning",
            "Withings webhook deduplication unavailable; fetching without a claim",
            provider="withings",
            action="webhook_dedup_unavailable",
            error=type(exc).__name__,
        )
        yield True
        return

    if owner is not None and owner != notification_id:
        yield False
        return

    try:
        yield True
    except Exception:
        try:
            get_redis_client().delete(key)
        except RedisError:
            # The claim expires on its own; losing the release only delays the retry.
            logger.warning("Withings webhook dedup claim could not be released", exc_info=True)
        raise
