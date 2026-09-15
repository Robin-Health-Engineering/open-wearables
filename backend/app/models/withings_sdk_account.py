from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import PrimaryKey


class WithingsSdkAccount(BaseDbModel):
    """Per-member state for the Withings Mobile SDK (phase 2).

    A table of its own rather than columns on ``user_connection``, for three reasons:

    * ``user_connection`` is an upstream table and this fork has to keep rebasing onto
      upstream cleanly; widening it invites a conflict on every rebase.
    * None of this means anything to the other twelve providers.
    * ``external_id`` is the identifier WE minted for this account, and the join back to the
      member; nothing in the other twelve providers has an equivalent.

    Hangs off ONE ``user_connection``, and a member can have several — their own linked
    Withings account, plus an account we created for each cellular order, because Withings
    creates an account on every provisioning path and a device cannot join one that already
    exists. So there is one of these rows per account we provisioned — at most two per member,
    theirs and ours.

    ``external_id`` is the value WE minted and is the join back to the member, and it is the bare
    CustomerProfile id: stable across that member's orders, because Withings reuse the account
    their first order created rather than making a new one each time (confirmed 2026-09-15). It
    briefly carried a ``#{orderRef}`` suffix on the opposite reading; which ORDER a shipment
    belongs to is ``customer_ref_id``, which this fork does not store.

    The column stays 128 rather than being narrowed back to 64. Widening it was the thing that
    needed a migration; leaving the headroom costs nothing and means a future id scheme does not
    need another one.
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
    # See the class docstring for why it is 128 and no longer the bare CustomerProfile id.
    external_id: Mapped[str] = mapped_column(String(128), unique=True)

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
