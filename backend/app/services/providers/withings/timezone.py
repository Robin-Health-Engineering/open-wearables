"""Convert Withings local dates and IANA zones to stored UTC instants and offsets."""

from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.utils.structured_logging import log_structured

_YMD = "%Y-%m-%d"


def _load_zone(
    timezone_name: str | None,
    logger: Logger,
    *,
    action: str,
    **context: Any,
) -> ZoneInfo | None:
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        log_structured(
            logger,
            "warning",
            "Invalid Withings timezone; falling back to UTC semantics",
            provider="withings",
            action=action,
            timezone=timezone_name,
            **context,
        )
        return None


def _format_offset(offset: timedelta | None) -> str | None:
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def zone_offset_at(
    timezone_name: str | None,
    utc_instant: datetime,
    logger: Logger,
    *,
    action: str,
    **context: Any,
) -> str | None:
    """Return the zone's canonical offset at an aware UTC instant."""
    zone = _load_zone(timezone_name, logger, action=action, **context)
    if zone is None:
        return None
    return _format_offset(utc_instant.astimezone(zone).utcoffset())


def local_day_start(
    local_date: str,
    timezone_name: str | None,
    logger: Logger,
    *,
    action: str,
    **context: Any,
) -> tuple[datetime, str | None] | None:
    """Resolve a local date to its UTC instant and offset, or ``None`` if invalid."""
    try:
        midnight = datetime.strptime(local_date, _YMD)
    except (ValueError, TypeError):
        return None
    zone = _load_zone(timezone_name, logger, action=action, **context)
    if zone is None:
        return midnight.replace(tzinfo=timezone.utc), None
    local_midnight = midnight.replace(tzinfo=zone)
    return local_midnight.astimezone(timezone.utc), _format_offset(local_midnight.utcoffset())
