"""Single-flight guard for Withings access-token refresh.

Withings rotates the refresh token on **every** refresh, and the previous one dies as
soon as the new access token is used. That makes a concurrent refresh materially worse
than for providers whose refresh token is stable:

  worker A ---- requesttoken(R0) ----> R1   persists R1
  worker B ---- requesttoken(R0) ----> R2   persists R2   (R1 now orphaned)

Whichever write lands last wins, and the connection is left holding a token whose
sibling rotation has already invalidated it. The user must reconnect - there is no
recovery path, because we never saw the token that is actually live.

This is reachable today: ``api_client._get_valid_token()`` refreshes whenever the token
expires within 5 minutes and runs in every Celery worker, while
``sync_coordination.try_become_primary`` only serialises the *pull* path. A webhook
-triggered fetch takes a different lock (``webhook_dedup.claim_fetch``), so a
notification arriving during a periodic sync is exactly the interleaving above.

The guard is deliberately Redis-based rather than a DB row lock: the refresh spans an
outbound HTTP call, and holding a Postgres row lock across that would pin a connection
from the pool for the duration of a third-party request.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

from app.integrations.redis_client import get_redis_client
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Longer than a slow requesttoken round trip, short enough that a worker killed
# mid-refresh does not wedge the connection for long.
_LOCK_TTL_SECONDS = 30

# How long a queued caller waits for the holder before giving up and refreshing itself.
_WAIT_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 0.25

# Only the holder may release, so a lock that already expired and was re-acquired by
# someone else is never deleted by the previous holder.
_RELEASE_IF_MINE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def refresh_lock_key(user_id: UUID) -> str:
    return f"withings:refresh:{user_id}"


@contextmanager
def single_flight_refresh(user_id: UUID) -> Iterator[bool]:
    """Serialise Withings token refresh for one user.

    Yields ``True`` when this caller holds the lock and should perform the refresh, and
    ``False`` when the wait timed out. A ``False`` caller must re-check the stored
    connection before refreshing anyway - by then the holder has usually rotated the
    token, and reusing its result is what avoids the double rotation.

    Fails **open**: if Redis is unreachable we yield ``True`` rather than blocking every
    refresh in the fleet. A degraded Redis should not take Withings sync down with it;
    the pre-flight re-read in the caller still narrows the window considerably.
    """
    try:
        client = get_redis_client()
    except Exception:  # noqa: BLE001 - Redis being down must not break refresh entirely
        log_structured(
            logger,
            "warning",
            "Withings refresh lock unavailable, proceeding unserialised",
            provider="withings",
            task="refresh_access_token",
            user_id=str(user_id),
        )
        yield True
        return

    key = refresh_lock_key(user_id)
    token = str(uuid4())
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    acquired = False

    while True:
        acquired = bool(client.set(key, token, nx=True, ex=_LOCK_TTL_SECONDS))
        if acquired or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    if not acquired:
        log_structured(
            logger,
            "warning",
            "Withings refresh lock wait timed out",
            provider="withings",
            task="refresh_access_token",
            user_id=str(user_id),
            waited_seconds=_WAIT_TIMEOUT_SECONDS,
        )

    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_RELEASE_IF_MINE, 1, key, token)
            except Exception:  # noqa: BLE001 - the TTL is the backstop
                log_structured(
                    logger,
                    "warning",
                    "Withings refresh lock release failed, relying on TTL",
                    provider="withings",
                    task="refresh_access_token",
                    user_id=str(user_id),
                )
