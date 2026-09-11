from datetime import datetime
from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64
from app.schemas.auth import ConnectionStatus


class UserConnection(BaseDbModel):
    """OAuth connections to external cloud providers (Suunto, Garmin, Polar, Coros)"""

    __table_args__ = (
        Index(
            "ix_user_connection_token_expiry",
            "token_expires_at",
            postgresql_where="status = 'active'",
        ),
        # One row per (member, provider, PROVIDER ACCOUNT) — not per (member, provider).
        #
        # Withings is why. A cellular device cannot be activated onto an account the partner did
        # not create, so a member who links their own Withings account and is then shipped a
        # device legitimately holds two accounts — and another for every later order. The
        # two-column form forbade that, and provisioning resolved the collision by overwriting,
        # which silently stopped the member's own scale and watch from syncing.
        #
        # Still unique, and deliberately: two rows for the SAME provider account on the same
        # member is a bug worth a constraint. It is also the guard if Withings ever ADOPTS an
        # existing account instead of creating one — that comes back as the same provider_user_id
        # and fails loudly here, rather than as two rows describing one account.
        #
        # NULLS NOT DISTINCT is load-bearing, not decoration. provider_user_id is nullable and
        # SDK-based providers (Apple) never set one; Postgres treats NULLs as distinct in a unique
        # index, so without this clause those connections would lose their uniqueness guarantee
        # entirely and ensure_sdk_connection could race into duplicates — a provider with nothing
        # to do with Withings, broken by a Withings change. Needs PG15+; we run 18.
        Index(
            "ix_user_connection_user_provider",
            "user_id",
            "provider",
            "provider_user_id",
            unique=True,
        ),
        Index("ix_user_connection_status_user_id", "status", "user_id"),
        Index(
            "ix_user_connection_provider_external_id",
            "provider",
            "provider_user_id",
            postgresql_where="provider_user_id IS NOT NULL AND status = 'active'",
        ),
    )
    __tablename__ = "user_connection"

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[str_64]  # 'suunto', 'garmin', 'polar', 'coros'

    # Provider user data
    provider_user_id: Mapped[str | None]
    provider_username: Mapped[str | None]

    # OAuth tokens (optional for SDK-based providers like Apple)
    access_token: Mapped[str | None]
    refresh_token: Mapped[str | None]
    token_expires_at: Mapped[datetime | None]
    scope: Mapped[str | None]

    # Metadata
    status: Mapped[ConnectionStatus]
    last_synced_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
