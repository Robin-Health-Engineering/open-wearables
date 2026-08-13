"""Provider-local ingestion outcomes for Withings data domains."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID

from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus
from app.services.providers.withings.applis import Domain


class IngestionResult(int):
    """An integer-compatible processed count with honest ingestion metrics."""

    processed: int
    write_counts: WriteCounts | None
    skipped: int
    failed: int

    def __new__(
        cls,
        processed: int,
        *,
        write_counts: WriteCounts | None = None,
        skipped: int = 0,
        failed: int = 0,
    ) -> IngestionResult:
        if min(processed, skipped, failed) < 0:
            raise ValueError("Ingestion result counts must not be negative")
        if write_counts is not None and min(write_counts.inserted, write_counts.updated) < 0:
            raise ValueError("Ingestion write counts must not be negative")
        if write_counts is not None and int(write_counts) != processed:
            raise ValueError("write counts must equal processed")

        result = super().__new__(cls, processed)
        result.processed = processed
        result.write_counts = write_counts
        result.skipped = skipped
        result.failed = failed
        return result

    @property
    def inserted(self) -> int:
        return self.write_counts.inserted if self.write_counts is not None else 0

    @property
    def updated(self) -> int:
        return self.write_counts.updated if self.write_counts is not None else 0

    @property
    def status(self) -> SyncStatus:
        if self.failed > 0:
            return SyncStatus.PARTIAL if self.processed > 0 else SyncStatus.FAILED
        if self.processed == 0:
            return SyncStatus.SKIPPED
        return SyncStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "items_processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        if self.write_counts is not None:
            result.update(inserted=self.inserted, updated=self.updated)
        return result

    @classmethod
    def coerce(cls, value: int) -> IngestionResult:
        return value if isinstance(value, cls) else cls(int(value))

    @classmethod
    def combine(cls, results: Iterable[IngestionResult]) -> IngestionResult:
        components = list(results)
        has_write_counts = bool(components) and all(result.write_counts is not None for result in components)
        return cls(
            sum(result.processed for result in components),
            write_counts=(
                WriteCounts(
                    sum(result.inserted for result in components),
                    sum(result.updated for result in components),
                )
                if has_write_counts
                else None
            ),
            skipped=sum(result.skipped for result in components),
            failed=sum(result.failed for result in components),
        )


@dataclass(frozen=True, slots=True)
class WithingsUserWebhookResult:
    """Typed outcome for one local user in a Withings webhook fan-out."""

    user_id: UUID
    domain: Domain
    components: Mapping[str, IngestionResult]
    combined: IngestionResult = field(init=False)

    def __post_init__(self) -> None:
        components = MappingProxyType(dict(self.components))
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "combined", IngestionResult.combine(components.values()))

    @property
    def status(self) -> SyncStatus:
        return self.combined.status

    @property
    def items_processed(self) -> int:
        return self.combined.processed

    @property
    def skipped(self) -> int:
        return self.combined.skipped

    @property
    def failed(self) -> int:
        return self.combined.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            **self.combined.to_dict(),
            "components": {name: result.to_dict() for name, result in self.components.items()},
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "skipped": self.skipped,
            "failed": self.failed,
            "components": {name: result.to_dict() for name, result in self.components.items()},
        }
