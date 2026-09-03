"""Coordinate the application-wide Withings request budget through Redis."""

import logging
import math
import time

from fastapi import HTTPException, status
from redis.exceptions import RedisError

from app.config import settings
from app.integrations.redis_client import get_redis_client
from app.utils.sentry_helpers import log_and_capture_error

logger = logging.getLogger(__name__)

_REDIS_KEY = "withings:request_budget:v1"

# Bound how long background callers queue; deadline-sensitive callers override it.
_MAX_QUEUE_SECONDS = 5

# Reserve globally ordered request slots using Redis' clock. This smooths the
# application-wide budget instead of allowing every worker its own burst.
_RESERVE_SLOT_LUA = """
local redis_time = redis.call('TIME')
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
local interval_ms = tonumber(ARGV[1])
local max_wait_ms = tonumber(ARGV[2])
local next_ms = tonumber(redis.call('GET', KEYS[1])) or now_ms
local slot_ms = math.max(now_ms, next_ms)
local wait_ms = slot_ms - now_ms
if wait_ms > max_wait_ms then
    return {-1, wait_ms}
end
redis.call('PSETEX', KEYS[1], max_wait_ms + interval_ms + 60000, slot_ms + interval_ms)
return {1, wait_ms}
"""


class WithingsRequestBudgetExceeded(HTTPException):
    """The distributed queue is full; callers should retry after the supplied delay."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Withings request budget exhausted; retry later",
            headers={"Retry-After": str(retry_after_seconds)},
        )


def acquire_request_slot(*, max_wait_seconds: float = _MAX_QUEUE_SECONDS) -> None:
    """Wait for a globally ordered Withings API slot.

    ``max_wait_seconds`` bounds how long the caller is willing to queue before
    getting rejected; override it when the caller has a different deadline.
    """
    interval_ms = math.ceil(60_000 / settings.withings_api_requests_per_minute)
    try:
        reserved, wait_ms = get_redis_client().eval(
            _RESERVE_SLOT_LUA,
            1,
            _REDIS_KEY,
            interval_ms,
            max_wait_seconds * 1000,
        )
    except RedisError as exc:
        # Fail closed rather than let every worker burst past the shared limit.
        log_and_capture_error(
            exc,
            logger,
            "Withings request budget unavailable",
            extra={"error": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings request budget unavailable",
        ) from exc

    if int(reserved) != 1:
        raise WithingsRequestBudgetExceeded(max(1, math.ceil(int(wait_ms) / 1000)))
    if wait_ms:
        time.sleep(int(wait_ms) / 1000)
