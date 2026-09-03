from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import PrimaryKey, str_64


class WithingsSdkAccount(BaseDbModel):
    """Per-member state for the Withings Mobile SDK (phase 2).

    A table of its own rather than columns on ``user_connection``, for three reasons:

    * ``user_connection`` is an upstream table and this fork has to keep rebasing onto
      upstream cleanly; widening it invites a conflict on every rebase.
    * None of this means anything to the other twelve providers.
    * ``csrf_token`` is only the first field. Device ``advertise_key``s land here next —
      Withings requires both sources of them (the install notification AND ``Getdevice``),
      and background BLE sync does not work without one.

    Distinct from the phase-1 consumer-OAuth connection by design: a member may link their
    own Withings account AND buy a device from us, giving two ``provider_user_id``s for one
    person. ``external_id`` is the value WE minted and is what ties this row back to the
    member, so it is unique.
    """

    __table_args__ = (
        # One SDK account per connection: a second row for the same connection would mean two
        # csrf_tokens for one token pair, and nothing could say which is current.
        Index("ix_withings_sdk_account_connection", "user_connection_id", unique=True),
    )
    __tablename__ = "withings_sdk_account"

    id: Mapped[PrimaryKey[UUID]]

    # NOT NULL and CASCADE, unlike the shared FKUserConnection alias (nullable, SET NULL):
    # this row is meaningless without its connection — the csrf_token belongs to that token
    # pair — so an orphan is never a state worth keeping.
    user_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_connection.id", ondelete="CASCADE"), nullable=False
    )

    # Ours, not Withings'. The value we sent to createuser and the join back to the member.
    external_id: Mapped[str_64] = mapped_column(unique=True)

    # Reissued on every token refresh, so it is as short-lived as the access token and must
    # be rewritten alongside it. Nullable because a row can exist between createuser and the
    # code exchange.
    csrf_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    updated_at: Mapped[datetime]
