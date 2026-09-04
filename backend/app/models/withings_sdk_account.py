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

    Hangs off whichever ``user_connection`` the member has for Withings. There is only ever
    one: that table's unique ``(user_id, provider)`` index means a personally-linked account
    and an SDK-provisioned one cannot coexist for the same member, and provisioning
    overwrites. ``external_id`` is the value WE minted and is what ties this row back to the
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
    # be rewritten alongside it.
    #
    # NULLABLE, and no path in today's code produces a null: ``provision_sdk_account`` only
    # writes this row after ``exchange_sdk_code`` has returned, and that exchange rejects a
    # response missing ``csrf_token`` outright. The looseness is deliberate anyway, for the
    # path being measured right now — if Withings issues a csrf_token on the refresh of a
    # connection we did NOT create (see the probe in ``oauth._persist_rotated_csrf_token``),
    # then an OAuth-linked member gets a row at link time and a token at the next refresh,
    # with a real gap in between. The 409 on the session route and the guard on the
    # provisioning route cover that gap; they are not dead code waiting on a bug.
    csrf_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    updated_at: Mapped[datetime]
