from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import PrimaryKey, str_32, str_64


class WithingsDevice(BaseDbModel):
    """One physical Withings device set up by a member through the Mobile SDK.

    Exists for ``advertise_key``. Background BLE sync is what makes a scale or a monitor
    useful between Wi-Fi sessions, and the SDK cannot start it without that per-device token
    — so a device row missing one is a device that silently stops reporting.

    Withings hands that token out two ways and states that **both must be implemented**: the
    install-success notification the app receives from the SDK, and ``User v2 - Getdevice``.
    Hence two writers (``sdk_devices``), one row, and ``advertise_key_source`` recording which
    of them last supplied the value — because when a device stops syncing, "where did this key
    come from" is the first question worth being able to answer.

    Its own table rather than columns on ``withings_sdk_account`` because the cardinality is
    different: one SDK account, many devices. And not on ``user_connection`` for the reason
    that table gives — it is upstream's, and this fork has to keep rebasing onto it cleanly.
    """

    __table_args__ = (
        # A member cannot own the same physical device twice. This is the upsert key for both
        # writers: without it, a notification and a Getdevice sync reporting the same device
        # produce two rows, and nothing says which advertise_key is current.
        Index("ix_withings_device_connection_device", "user_connection_id", "device_id", unique=True),
    )
    __tablename__ = "withings_device"

    id: Mapped[PrimaryKey[UUID]]

    # NOT NULL and CASCADE, matching withings_sdk_account: a device belongs to a connection,
    # and a device row that outlived it could never be synced, read or dissociated again.
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

    # The BLE token background sync needs. NULLABLE, and that is a real state rather than a
    # gap in the schema: a Wi-Fi device that never fell back to BLE has no reason to have one,
    # and a device known only from a Getdevice response may not carry one either. Absent means
    # "no background sync for this device", which is a thing the app has to be able to say.
    advertise_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Which writer last supplied advertise_key: "notification" or "getdevice". Diagnostic, not
    # a rule — neither source is authoritative over the other, and the later write wins.
    advertise_key_source: Mapped[str_32 | None] = mapped_column(nullable=True)

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
    # It needs its own column rather than a reading of ``advertise_key_source``: a Getdevice
    # entry that carries no ``advertise_key`` leaves that field saying "notification", so a
    # sweep keyed on it would still sweep devices Getdevice knows perfectly well about.
    last_getdevice_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Set when the member dissociates the device in Withings' settings WebView, and when a
    # Getdevice sync stops listing a device we hold.
    #
    # SOFT on purpose. Neither source is complete — a Getdevice response that transiently
    # omits a device would, under a hard delete, destroy an advertise_key that only the
    # install notification ever carried and that nothing can re-derive. A device that comes
    # back simply has this cleared again.
    dissociated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    updated_at: Mapped[datetime]
