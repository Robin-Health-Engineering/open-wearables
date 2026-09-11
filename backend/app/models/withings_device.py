from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import PrimaryKey, str_32, str_64


class WithingsDevice(BaseDbModel):
    """One Withings device on a member's account, as the member is shown it.

    This table was created for ``advertise_key`` — the per-device token the Withings Mobile SDK
    needed to start background BLE sync. That integration is abandoned: cellular devices ship
    already connected, so there is no pairing to bridge and no key to carry. Both columns are
    gone, and what the table is FOR now is the answer to "which devices do I have, and are they
    working": model, type, when each last synced, how its battery is doing.

    It is deliberately NOT the record of what we shipped. Order state — the order id, its
    shipment status and history, the delivery address, the device MAC — lives in robin-backend's
    DynamoDB, because it is commerce rather than health data and this is a fork that has to keep
    rebasing onto upstream. The two sides answer different questions and both are legitimate:
    this table is authoritative for what Withings currently reports on the account, that one for
    what we ordered. ``order_ref`` is the link between them.

    Its own table rather than columns on ``withings_sdk_account`` because the cardinality is
    different: one account, many devices. And not on ``user_connection`` for the reason that
    table gives — it is upstream's.
    """

    __table_args__ = (
        # A member cannot own the same physical device twice on one account, and this is the
        # upsert key for the Getdevice sweep.
        Index("ix_withings_device_connection_device", "user_connection_id", "device_id", unique=True),
    )
    __tablename__ = "withings_device"

    id: Mapped[PrimaryKey[UUID]]

    # NOT NULL and CASCADE, matching withings_sdk_account: a device belongs to a connection,
    # and a device row that outlived it could never be synced, read or dissociated again.
    # Which connection also says which Withings account it is on — a member can have several.
    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"), nullable=False
    )

    # Withings' ``deviceid``. Theirs, not ours, and opaque — do not parse it.
    device_id: Mapped[str_64]

    # Withings' numeric model (6 = Body+, 45 = BPM Connect, …) and its display name. The
    # numeric one is what the setup WebView takes as ``device_model``; the name is only for
    # showing a member which of their devices this is.
    model_id: Mapped[int | None] = mapped_column(nullable=True)
    model: Mapped[str_64 | None] = mapped_column(nullable=True)

    # "Scale", "Blood Pressure Monitor", "Sleep Monitor" — Withings' own vocabulary.
    device_type: Mapped[str_32 | None] = mapped_column(nullable=True)

    # Withings' own word for the charge level ("high", "medium", "low"). Getdevice has always
    # returned it and we parsed it into nothing; on a screen listing a member's devices it is
    # the field that answers why a scale stopped reporting. Nullable because not every device
    # reports one, which is why every field of that response but ``deviceid`` is optional.
    battery: Mapped[str_32 | None] = mapped_column(nullable=True)

    # The robin-backend order this device shipped on, and the join to its DynamoDB row (status,
    # address, MAC). NULLABLE and expected to be: a device the member already owned when they
    # linked their own Withings account arrived through no order of ours.
    order_ref: Mapped[str_64 | None] = mapped_column(nullable=True)

    # From Getdevice's ``last_session_date``. What "last synced" on the device hub is built
    # from, and the honest answer to "why has nothing arrived from my scale in a week".
    last_session_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # When a Getdevice response last LISTED this device. Null means it never has.
    #
    # This is what makes the dissociation sweep safe. A device Getdevice has never listed
    # says nothing by being absent from it — Getdevice may not list a just-installed device
    # yet, which is the whole reason the install notification is a separate source. Sweeping
    # on absence alone marks a scale the member paired seconds ago as dissociated.
    #
    # It needs a column of its own: "has Getdevice ever listed this device" is not derivable
    # from any other field here, since every one of them can be filled in by the install
    # notification alone.
    last_getdevice_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Set when a Getdevice sync stops listing a device we hold.
    #
    # SOFT on purpose: a response that transiently omits a device would, under a hard delete,
    # lose the row's history — when it last synced, which order it came from — none of which
    # Getdevice can re-derive. A device that comes back simply has this cleared again.
    dissociated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    updated_at: Mapped[datetime]
