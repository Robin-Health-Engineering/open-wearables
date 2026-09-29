"""Group-level facts about Withings measures: which device, which account, and whether it is new.

See ``WithingsMeasureGroupRecord`` for why these live beside the samples rather than on them.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import WithingsMeasureGroupRecord
from app.schemas.enums import SeriesType
from app.schemas.providers.withings import WithingsMeasureGroup
from app.services.providers.withings.coverage import MEASURE_TYPE_MAP

# SeriesType -> the metric key Robin and the app use (spec Appendix C2). Every C2 key is producible
# since Task 3 added visceral fat (170) and BMR (226).
C2_KEYS: dict[SeriesType, str] = {
    SeriesType.weight: "weight",
    SeriesType.body_fat_percentage: "fat_ratio",
    SeriesType.body_fat_mass: "fat_mass",
    SeriesType.skeletal_muscle_mass: "muscle_mass",
    SeriesType.body_water_mass: "hydration",
    SeriesType.bone_mass: "bone_mass",
    SeriesType.heart_rate: "heart_rate",
    SeriesType.withings_pulse_wave_velocity: "pulse_wave_velocity",
    SeriesType.cardiovascular_age: "vascular_age",
    SeriesType.withings_visceral_fat: "visceral_fat",
    SeriesType.withings_basal_metabolic_rate: "basal_metabolic_rate",
}


@dataclass(frozen=True)
class ParsedGroup:
    grpid: str
    device_id: str | None
    model: str | None
    attrib: int | None
    measured_at: datetime
    # C2 keys present in this group, in measure order, deduplicated. Empty when the group only
    # holds measures outside C2 (e.g. blood pressure): still recorded, but there is nothing to emit.
    metric_keys: tuple[str, ...]

    @property
    def has_c2_metrics(self) -> bool:
        return bool(self.metric_keys)


def parsed_group_of(group: WithingsMeasureGroup) -> ParsedGroup | None:
    """The group-level facts, or None when there is nothing to attribute.

    No grpid means no join to the samples; no mapped measure means nothing was stored.
    """
    if group.grpid is None:
        return None
    series = [MEASURE_TYPE_MAP[m.type] for m in group.measures if m.type in MEASURE_TYPE_MAP]
    if not series:
        return None
    keys = tuple(dict.fromkeys(C2_KEYS[s] for s in series if s in C2_KEYS))
    return ParsedGroup(
        grpid=str(group.grpid),
        device_id=group.deviceid,
        model=group.model,
        attrib=group.attrib,
        measured_at=datetime.fromtimestamp(group.date, tz=timezone.utc),
        metric_keys=keys,
    )


def record_new_groups(
    db: DbSession, *, user_id: UUID, user_connection_id: UUID, groups: list[ParsedGroup]
) -> list[ParsedGroup]:
    """Insert the groups not yet recorded for this connection and return exactly those.

    Idempotent on ``(user_connection_id, grpid)``: a re-read window, a redelivered notification
    or a sibling appli notification for the same weigh-in returns nothing the second time. Groups
    without C2 metrics are recorded too (callers skip them via ``has_c2_metrics``). Does not
    commit; the caller commits alongside the samples.
    """
    if not groups:
        return []
    by_grpid = {g.grpid: g for g in groups}
    stmt = (
        insert(WithingsMeasureGroupRecord)
        .values(
            [
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "user_connection_id": user_connection_id,
                    "grpid": g.grpid,
                    "device_id": g.device_id,
                    "model": g.model,
                    "attrib": g.attrib,
                    "measured_at": g.measured_at,
                }
                for g in by_grpid.values()
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_connection_id", "grpid"])
        .returning(WithingsMeasureGroupRecord.grpid)
    )
    inserted = {row[0] for row in db.execute(stmt)}
    return [g for g in by_grpid.values() if g.grpid in inserted]
