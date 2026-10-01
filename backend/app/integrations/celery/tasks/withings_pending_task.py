"""Daily: retire pending Withings weigh-ins nobody answered within 7 days (spec 2026-10-01 D4)."""

from celery import shared_task

from app.database import SessionLocal
from app.services.providers.withings.attribution import RETIRE_STALE_PENDING_TASK, retire_stale_pending


@shared_task(name=RETIRE_STALE_PENDING_TASK)
def retire_stale_pending_readings() -> dict[str, int]:
    with SessionLocal() as db:
        return {"retired": retire_stale_pending(db)}
