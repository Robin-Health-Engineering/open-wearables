from enum import StrEnum


class LiveSyncMode(StrEnum):
    PULL = "pull"
    WEBHOOK = "webhook"


def resolve_live_sync_mode(
    configured: LiveSyncMode | str | None,
    default: LiveSyncMode | str | None,
) -> LiveSyncMode | None:
    """Return the configured override, otherwise the provider default.

    ``None`` from both inputs means the provider has no server-side live-sync
    mode; callers should compare against the explicit mode they require.
    """
    mode = configured if configured is not None else default
    return LiveSyncMode(mode) if mode is not None else None
