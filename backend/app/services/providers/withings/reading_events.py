"""``withings.reading.created``: tell Robin a purchased device recorded a new reading.

The one place OW calls robin-backend (spec: "OW -> Robin signed event", a documented exception to
Robin's no-callback rule). It carries ids and metric NAMES only, never values; Robin reads the
reading back through the readings API, authenticated as it always is.

Sent only for groups that are
* NEW, i.e. just inserted into withings_measure_group (``record_new_groups``);
* on a connection WE provisioned (a ``withings_sdk_account`` row), because a member's own
  Withings account is not the device we sold them;
* device-captured (a ``deviceid``, attrib not 2/4 = manual entry). An ambiguous ``attrib 1``
  weigh-in on a provisioned account is recorded ``pending`` (spec 2026-10-01 D1) and IS announced,
  with ``pending: true``, so Robin asks the member "È tua questa pesata?"; a ``discarded`` group
  (the late sibling of a weigh-in the member already rejected) never is;
* carrying at least one C2 metric (a blood-pressure-only group has nothing to announce);
* RECENT (``robin_reading_event_max_age_hours``), because backfills re-read history;
* ONE PER SESSION (``measure_groups.session_device``): a cellular Body Pro 2 weigh-in arrives as
  a body-composition group plus a heart-pulse-only group with the same date and device. Only the
  session's representative (the weight group, else the lowest grpid) is announced, with ``types``
  the union of the session's C2 keys.

  Known edge: if the pulse group arrives ALONE in an earlier ingest and the weight group in a
  later one, the pulse group is announced (under its own grpid) and the weight group is not. The
  readings API still shows the whole weigh-in under either grpid, so the push opens the right
  reading; only its ``types`` and grpid differ from the usual.

EXACTLY ONE DECISION per session that has an announceable group, across ingests too; AT MOST ONE
DELIVERY. Ingest takes the session's lock (``measure_groups.lock_sessions``) before it records the
groups and holds it through this module's DECISION (``decide_reading_events``), which runs before
its commit. A later ingest of the same weigh-in (the pulse group Withings exposed a moment after
the body group) waits on that lock, then sees the earlier ingest's committed groups, and decides
nothing if one of them was announceable: that ingest DECIDED to announce the session. A pending
session's one event carries ``pending: true``; a late sibling of a pending or discarded session
announces nothing (R10). The SEND (``send_reading_events``) runs after the commit. Both steps are
best effort and never fail the ingest, and the groups are committed between them.

What a failure costs: if the decision or the enqueue fails, the session gets no push. The reading
stays visible in the readings API; nothing retries (no later ingest, no recovery job), because the
committed groups make every later ingest treat the session as already decided. The failure goes
to Sentry (``log_and_capture_error``, in both the decision and the enqueue). Nothing is stored
that tells "announced" from "announceable".

Enqueue is by name (``DELIVER_TASK``), so there is no import dependency on the Celery task; the
task calls ``post_event`` here, which signs and POSTs.
"""

import dataclasses
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

import httpx
from celery import current_app as celery_app
from sqlalchemy import and_, or_

from app.config import settings
from app.database import DbSession
from app.models import DataPointSeries, DataSource, WithingsMeasureGroupRecord
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName, get_series_type_id
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.withings.measure_groups import (
    C2_KEYS,
    DISCARDED,
    PENDING,
    REGISTERED,
    WEIGHT_KEY,
    ParsedGroup,
    representative_grpid,
    session_device,
    session_device_column,
)
from app.services.providers.withings.sdk_devices import sync_devices_from_withings
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

EVENT_NAME = "withings.reading.created"
DELIVER_TASK = "app.integrations.celery.tasks.withings_reading_event_task.deliver_withings_reading_event"
_MANUAL_ATTRIBS = frozenset({2, 4})

DeliveryOutcome = Literal["delivered", "rejected", "retry", "disabled"]
# Bounded so a hung Robin cannot pin a worker. Robin's API Gateway limit is 29 s (its Lambda 25 s), so 28 s.
_TIMEOUT_SECONDS = 28.0


def _endpoint() -> tuple[str, str] | None:
    """(url, secret) when delivery is configured, else None.

    An empty or blank value counts as UNSET: an env var left as "" must not enable delivery,
    and above all must not sign with an empty key.
    """
    url = (settings.robin_reading_event_url or "").strip()
    secret_str = settings.robin_reading_event_secret
    secret = secret_str.get_secret_value() if secret_str is not None else ""
    if not url or not secret.strip():
        return None
    return url, secret


def is_enabled() -> bool:
    return _endpoint() is not None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(
    *, external_user_id: str, withings_user_id: str | None, group: ParsedGroup, hash_deviceid: str | None = None
) -> dict[str, Any]:
    return {
        "event": EVENT_NAME,
        "external_user_id": external_user_id,
        "withings_user_id": withings_user_id,
        "device_id": group.device_id,
        "hash_deviceid": hash_deviceid,
        "grpid": group.grpid,
        "measured_at": _iso_utc(group.measured_at),
        "types": list(group.metric_keys),
        # Contract A3: true iff the session is held pending (attrib 1, awaiting the member's answer).
        "pending": group.status == PENDING,
    }


def _eligible(group: ParsedGroup, cutoff: datetime) -> bool:
    """Device-captured, not discarded, with something to announce, and recent.

    Manual entries (attrib 2 and 4) are refused. Ambiguous ones (attrib 1) are accepted: on an
    account we provisioned they arrive ``pending`` and the event says so, so the member is asked
    rather than handed another person's weigh-in; on the member's own account they are registered
    as before, but that account never emits. A discarded group is the late sibling of a weigh-in
    the member already rejected: nothing to say.
    """
    return (
        group.device_id is not None
        and group.attrib not in _MANUAL_ATTRIBS
        and group.status != DISCARDED
        and group.has_c2_metrics
        and group.measured_at >= cutoff
    )


_C2_TYPE_IDS = [get_series_type_id(series) for series in C2_KEYS]


def _sessions(groups: list[ParsedGroup]) -> list[ParsedGroup]:
    """One group per session, in batch order: the representative, carrying the union of the C2 keys."""
    by_session: dict[tuple[str | None, datetime], list[ParsedGroup]] = {}
    for group in groups:
        by_session.setdefault((session_device(group.hash_device_id, group.device_id), group.measured_at), []).append(
            group
        )
    out = []
    for members in by_session.values():
        rep_grpid = representative_grpid({g.grpid: WEIGHT_KEY in g.metric_keys for g in members})
        rep = next(g for g in members if g.grpid == rep_grpid)
        keys = dict.fromkeys(rep.metric_keys)
        for group in members:
            keys.update(dict.fromkeys(group.metric_keys))
        out.append(dataclasses.replace(rep, metric_keys=tuple(keys)))
    return out


def _announced_earlier(
    db: DbSession, *, user_id: UUID, user_connection_id: UUID, session: ParsedGroup, batch_grpids: set[str]
) -> bool:
    """Whether an EARLIER ingest recorded a sibling of this session that was announceable, and so decided it.

    "And so decided it" holds because the caller holds the session lock: every earlier ingest of
    this session decided under the same lock and committed before this one could record, so the
    first ingest that recorded an announceable group found none earlier and decided to announce the
    session. That is a decision, not a delivery: it does not say the event was sent.
    Without the lock two ingests could each see the other's group here and neither send.

    Announceable as ``_eligible`` sees it from what is stored: a device-captured group (a
    ``deviceid``; a hash alone does not do) with at least one C2 sample. Recency needs no check,
    the sibling has the session's own time. A sibling that only held, say, blood pressure was
    never announced, so it does not silence this one.

    A pending or discarded sibling has no samples by design, so its status stands in for them: the
    weigh-in was announced (pending) or already answered (discarded). Known edge, accepted: a
    pending sibling that held only non-C2 measures silences this one too.
    """
    device = session_device(session.hash_device_id, session.device_id)
    group = WithingsMeasureGroupRecord
    same_session = (
        group.user_connection_id == user_connection_id,
        group.measured_at == session.measured_at,
        session_device_column() == device,
        group.grpid.not_in(batch_grpids),
        or_(group.attrib.is_(None), group.attrib.not_in(_MANUAL_ATTRIBS)),
    )
    held = db.query(group.id).filter(*same_session, group.status != REGISTERED).first()
    if held is not None:
        return True
    row = (
        db.query(group.id)
        .join(DataSource, and_(DataSource.user_id == user_id, DataSource.provider == ProviderName.WITHINGS))
        .join(
            DataPointSeries,
            and_(
                DataPointSeries.data_source_id == DataSource.id,
                DataPointSeries.external_id == group.grpid,
                DataPointSeries.recorded_at == group.measured_at,
                DataPointSeries.series_type_definition_id.in_(_C2_TYPE_IDS),
            ),
        )
        # Device-captured, as ``_eligible`` requires: a hash-only sibling was never announced.
        .filter(*same_session, group.device_id.is_not(None))
        .first()
    )
    return row is not None


def _devices(db: DbSession, user_connection_id: UUID, device_ids: set[str]) -> dict[str, tuple[str | None, bool]]:
    """device_id -> (stored hash, whether Getdevice has ever listed it). Absent = no row yet."""
    rows = db.query(WithingsDevice.device_id, WithingsDevice.hash_device_id, WithingsDevice.last_getdevice_at).filter(
        WithingsDevice.user_connection_id == user_connection_id, WithingsDevice.device_id.in_(device_ids)
    )
    return {device_id: (hash_device_id, listed_at is not None) for device_id, hash_device_id, listed_at in rows}


def _needs_refresh(devices: dict[str, tuple[str | None, bool]], device_ids: set[str]) -> bool:
    """A device with no row, or a row Getdevice has never listed, and no hash.

    A device Getdevice HAS listed without a hash simply does not report one: asking again on
    every reading would cost a Getdevice call per weigh-in and change nothing.
    """
    for device_id in device_ids:
        hash_device_id, listed = devices.get(device_id, (None, False))
        if hash_device_id is None and not listed:
            return True
    return False


def _rollback(db: DbSession, user_connection_id: UUID) -> None:
    """Leave the caller's session usable; a failing rollback must not escape either."""
    try:
        db.rollback()
    except Exception as rollback_error:
        log_and_capture_error(
            rollback_error,
            logger,
            "Withings reading event rollback failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )


def _refresh_hashes(db: DbSession, *, user_id: UUID, user_connection_id: UUID, oauth: BaseOAuthTemplate) -> None:
    """One Getdevice sweep of this connection, so a first reading can carry its hash_deviceid.

    A best effort: on failure the event still goes out with a null hash (Robin then falls back to
    the provisioned-account check), so this logs and swallows.
    """
    try:
        sync_devices_from_withings(db, user_id=user_id, oauth=oauth, connection_id=user_connection_id)
    except Exception as e:
        _rollback(db, user_connection_id)
        log_and_capture_error(
            e,
            logger,
            "Withings Getdevice refresh for a reading event failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )


@dataclass(frozen=True)
class ReadingEventPlan:
    """The sessions an ingest decided to announce, and who they are for: what ``send_reading_events`` needs."""

    user_id: UUID
    user_connection_id: UUID
    external_user_id: str
    # JSON null rather than the string "None" when Withings never reported a userid.
    withings_user_id: str | None
    # One per session: its representative, carrying the union of the session's C2 keys.
    sessions: list[ParsedGroup]


def decide_reading_events(
    db: DbSession, *, user_connection_id: UUID, groups: list[ParsedGroup], now: datetime | None = None
) -> ReadingEventPlan | None:
    """Which of this ingest's sessions get an event (see the module doc); None when none does.

    Runs inside the ingest's transaction, after ``record_new_groups`` and before the commit, while
    the ingest holds the session locks (``measure_groups.lock_sessions``): an earlier ingest of the
    same weigh-in has then committed its groups AND its decision, and a later one cannot record
    until this one commits. ``groups`` is exactly what this ingest inserted, so a sibling recorded
    under any other grpid came from an earlier ingest.

    Database reads only: no HTTP, no commit. Never raises: a failure is captured (Sentry) and rolled
    back to a savepoint, so the ingest's groups and samples still commit, nothing is announced, and
    nothing retries it (see the module doc).
    """
    if not groups or not is_enabled():
        return None
    try:
        with db.begin_nested():
            account = (
                db.query(WithingsSdkAccount)
                .filter(WithingsSdkAccount.user_connection_id == user_connection_id)
                .one_or_none()
            )
            if account is None:
                return None
            connection = UserConnectionRepository().get(db, user_connection_id)
            if connection is None:
                return None
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=settings.robin_reading_event_max_age_hours)
            batch_grpids = {g.grpid for g in groups}
            sessions = [
                session
                for session in _sessions([g for g in groups if _eligible(g, cutoff)])
                if not _announced_earlier(
                    db,
                    user_id=connection.user_id,
                    user_connection_id=user_connection_id,
                    session=session,
                    batch_grpids=batch_grpids,
                )
            ]
            if not sessions:
                return None
            return ReadingEventPlan(
                user_id=connection.user_id,
                user_connection_id=user_connection_id,
                external_user_id=account.external_id,
                withings_user_id=str(connection.provider_user_id) if connection.provider_user_id is not None else None,
                sessions=sessions,
            )
    except Exception as e:
        log_and_capture_error(
            e,
            logger,
            "Withings reading event decision failed",
            extra={"provider": "withings", "user_connection_id": str(user_connection_id)},
        )
        return None


def _event_hashes(db: DbSession, plan: ReadingEventPlan, oauth: BaseOAuthTemplate | None) -> list[str | None]:
    """Each session's ``hash_deviceid``: the group's own when it carries one, else the stored device row's.

    A group carrying its own hash needs no lookup: its deviceid may not even be one Getdevice lists
    (the cellular Body Pro 2 sends an integer there), so the hash is the only reliable join. Only
    hashless groups fall back to the stored device row, and ``oauth`` lets the first reading from a
    device that has no stored hash yet refresh Getdevice, at most once per call.
    """
    device_ids = {g.device_id for g in plan.sessions if g.device_id is not None and not g.hash_device_id}
    stored: dict[str, str | None] = {}
    if device_ids:
        devices = _devices(db, plan.user_connection_id, device_ids)
        if oauth is not None and _needs_refresh(devices, device_ids):
            # The first reading from a device can beat the Getdevice sweep that stores its hash.
            _refresh_hashes(db, user_id=plan.user_id, user_connection_id=plan.user_connection_id, oauth=oauth)
            devices = _devices(db, plan.user_connection_id, device_ids)
        stored = {device_id: devices.get(device_id, (None, False))[0] for device_id in device_ids}
    return [group.hash_device_id or stored.get(group.device_id or "") for group in plan.sessions]


def send_reading_events(db: DbSession, plan: ReadingEventPlan | None, *, oauth: BaseOAuthTemplate | None = None) -> int:
    """Enqueue one delivery per decided session; returns how many. After the ingest's commit. Never raises.

    At most one delivery per session: a failed enqueue is captured (Sentry) and not retried, so
    that session gets no push (see the module doc).

    After the commit because Robin reads the reading back as soon as it gets the event, and because
    the Getdevice refresh (``oauth``) is an HTTP call that must not run under the session locks.
    Without ``oauth`` the hash is whatever is stored, possibly null.
    """
    if plan is None:
        return 0
    try:
        hashes = _event_hashes(db, plan, oauth)
    except Exception as e:
        _rollback(db, plan.user_connection_id)
        log_and_capture_error(
            e,
            logger,
            "Withings reading event enqueue failed",
            extra={"provider": "withings", "user_connection_id": str(plan.user_connection_id)},
        )
        return 0

    sent = 0
    for group, hash_deviceid in zip(plan.sessions, hashes, strict=True):
        # Per group: the groups are already committed as seen, so one failed enqueue must not
        # drop the rest of the batch with it.
        try:
            payload = build_payload(
                external_user_id=plan.external_user_id,
                withings_user_id=plan.withings_user_id,
                group=group,
                hash_deviceid=hash_deviceid,
            )
            celery_app.send_task(DELIVER_TASK, args=[payload], queue="default")
            sent += 1
        except Exception as e:
            log_and_capture_error(
                e,
                logger,
                "Withings reading event enqueue failed",
                extra={
                    "provider": "withings",
                    "user_connection_id": str(plan.user_connection_id),
                    "grpid": group.grpid,
                },
            )
    log_structured(
        logger,
        "info",
        "Withings reading events enqueued",
        provider="withings",
        action="reading_event_enqueued",
        user_connection_id=str(plan.user_connection_id),
        count=sent,
        failed=len(plan.sessions) - sent,
        without_hash=sum(1 for h in hashes if h is None),
    )
    return sent


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """``X-Robin-Signature`` value: ``t=<ts>,v1=<lowercase hex HMAC-SHA256(secret, "<ts>.<body>")>``."""
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def post_event(payload: dict[str, Any], *, now: int | None = None) -> DeliveryOutcome:
    """POST one event. The timestamp is minted per ATTEMPT so a retry stays inside Robin's window.

    Retry on network errors, 5xx and 429 (API Gateway throttling is transient, and the request is
    fine). Robin answers 2xx for every well-formed signed event, including ones it drops, so any
    other 3xx/4xx means OUR request is wrong (bad secret, clock skew, wrong URL) and retrying cannot
    fix it. Never logs the secret, the signature or the external_user_id.
    """
    grpid = payload.get("grpid")
    endpoint = _endpoint()
    if endpoint is None:
        log_structured(
            logger,
            "info",
            "Robin reading event delivery disabled: url or secret unset",
            provider="withings",
            action="reading_event_disabled",
            grpid=grpid,
        )
        return "disabled"
    url, secret = endpoint
    # Serialise once and send exactly these bytes: the signature covers the raw body.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = now if now is not None else int(time.time())
    try:
        response = httpx.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Robin-Signature": sign(secret, timestamp, body),
            },
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        log_structured(
            logger,
            "warning",
            "Robin reading event delivery failed",
            provider="withings",
            action="reading_event_network_error",
            grpid=grpid,
            error=type(e).__name__,
        )
        return "retry"
    status = response.status_code
    if status >= 500 or status == 429:
        log_structured(
            logger,
            "warning",
            "Robin reading event delivery got a server error or throttling",
            provider="withings",
            action="reading_event_server_error",
            grpid=grpid,
            status=status,
        )
        return "retry"
    if status >= 300:
        log_and_capture_error(
            RuntimeError(f"Robin rejected a reading event with HTTP {status}"),
            logger,
            "Robin rejected a reading event",
            extra={"provider": "withings", "action": "reading_event_rejected", "grpid": grpid, "status": status},
        )
        return "rejected"
    return "delivered"
