from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_32, str_64

# Spec 2026-10-01 (D1, D4, D5): a session is registered (as every reading was before), pending (an
# ambiguous ``attrib 1`` weigh-in awaiting the member's answer) or discarded (answered "no", removed
# as "not me", or left unanswered for 7 days). Set per session: a weigh-in's groups share it.
ReadingStatus = Literal["registered", "pending", "discarded"]
REGISTERED: ReadingStatus = "registered"
PENDING: ReadingStatus = "pending"
DISCARDED: ReadingStatus = "discarded"


class WithingsMeasureGroupRecord(BaseDbModel):
    """One Withings measurement group (``measuregrp``) as it arrived on one connection.

    Samples live in ``data_point_series`` keyed by ``external_id = grpid``, but a sample cannot say
    which DEVICE took it: Withings' ``deviceid`` lives on the group, and putting it on the samples'
    ``data_source.device_model`` would open a second data source, so every re-read of an old window
    would insert duplicates. This table carries the group-level facts instead:

    * **which device**: ``device_id`` is the group's own opaque ``deviceid``. For most devices that
      is the value ``withings_device.device_id`` holds, but not for all: the cellular Body Pro 2
      sends an integer Getdevice never lists. ``hash_device_id`` is the group's ``hash_deviceid``,
      which does match ``withings_device.device_id``/``hash_device_id`` (and Robin's order);
    * **which account**: ``user_connection_id``. A member can hold two Withings connections, and
      only readings on the one WE provisioned belong to the device we sold them;
    * **whether it is new**: the unique ``(user_connection_id, grpid)`` makes the insert idempotent,
      so ``ON CONFLICT DO NOTHING RETURNING`` names exactly the groups never seen before. That is
      what the reading event is sent for;
    * **whether it is the member's**: ``status`` (see ``ReadingStatus``). A pending or discarded
      group has NO samples, and ingest never writes any for it, on any re-read. ``raw`` holds a
      pending group's payload as received, so confirming needs no Withings call; it is NULL
      otherwise.

    Named ``…Record`` because ``WithingsMeasureGroup`` is already the Pydantic payload schema.
    """

    __tablename__ = "withings_measure_group"
    __table_args__ = (
        Index("uq_withings_measure_group_connection_grpid", "user_connection_id", "grpid", unique=True),
        Index("ix_withings_measure_group_device_time", "user_connection_id", "device_id", "measured_at"),
        CheckConstraint(
            "status IN ('registered', 'pending', 'discarded')",
            name="ck_withings_measure_group_status",
        ),
        # The daily retirement scans pending groups by age; there are only ever a handful.
        Index(
            "ix_withings_measure_group_pending_measured_at",
            "measured_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    # NOT NULL and CASCADE, matching withings_device: a group without its connection can never be
    # attributed to an account again.
    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"), nullable=False
    )
    grpid: Mapped[str_32]
    device_id: Mapped[str_64 | None] = mapped_column(nullable=True)
    # Null for groups recorded before the column existed and for groups that carry no hash.
    hash_device_id: Mapped[str_64 | None] = mapped_column(nullable=True)
    model: Mapped[str_64 | None] = mapped_column(nullable=True)
    # Withings' capture attribution: 0/8 device-captured, 1 device-captured but ambiguous user,
    # 2/4 manual entry.
    attrib: Mapped[int | None] = mapped_column(nullable=True)
    measured_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REGISTERED, server_default=REGISTERED)
    # none_as_null: clearing it stores SQL NULL, not the JSON literal null.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
