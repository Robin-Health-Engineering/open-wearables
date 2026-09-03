"""Provider-local ingestion outcomes for Withings data domains."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID

from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus
from app.services.providers.withings.applis import Domain


def write_status(counts: WriteCounts) -> SyncStatus:
    """Classify one component's write outcome for the sync log."""
    if counts.failed > 0:
        return SyncStatus.PARTIAL if int(counts) > 0 else SyncStatus.FAILED
    if int(counts) == 0:
        return SyncStatus.SKIPPED
    return SyncStatus.SUCCESS


def write_summary(counts: WriteCounts) -> dict[str, Any]:
    """Render one component's counts for sync-log metadata."""
    summary: dict[str, Any] = {
        "status": write_status(counts).value,
        "items_processed": int(counts),
        "skipped": counts.skipped,
        "failed": counts.failed,
    }
    if counts.split_known:
        summary.update(inserted=counts.inserted, updated=counts.updated)
    return summary


@dataclass(frozen=True, slots=True)
class WithingsUserWebhookResult:
    """Typed outcome for one local user in a Withings webhook fan-out."""

    user_id: UUID
    domain: Domain
    components: Mapping[str, WriteCounts]
    combined: WriteCounts = field(init=False)

    def __post_init__(self) -> None:
        components = MappingProxyType(dict(self.components))
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "combined", WriteCounts.combine(components.values()))

    @property
    def status(self) -> SyncStatus:
        return write_status(self.combined)

    @property
    def items_processed(self) -> int:
        return int(self.combined)

    @property
    def skipped(self) -> int:
        return self.combined.skipped

    @property
    def failed(self) -> int:
        return self.combined.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            **write_summary(self.combined),
            "components": {name: write_summary(counts) for name, counts in self.components.items()},
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "skipped": self.skipped,
            "failed": self.failed,
            "components": {name: write_summary(counts) for name, counts in self.components.items()},
        }
