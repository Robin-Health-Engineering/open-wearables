from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, case, func, select, tuple_, update
from sqlalchemy.orm import Query
from sqlalchemy.orm.exc import MultipleResultsFound

from app.database import DbSession
from app.models import UserConnection
from app.repositories.repositories import CrudRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import SdkConnectionOutcome
from app.schemas.model_crud.user_management import (
    UserConnectionCreate,
    UserConnectionUpdate,
)

logger = getLogger(__name__)


class UserConnectionRepository(CrudRepository[UserConnection, UserConnectionCreate, UserConnectionUpdate]):
    """Repository for managing OAuth user connections to fitness providers."""

    def __init__(self, model: type[UserConnection] = UserConnection):
        super().__init__(model)

    def get_active_count(self, db_session: DbSession) -> int:
        """Get total count of active connections."""
        return (
            db_session.query(func.count(self.model.id)).filter(self.model.status == ConnectionStatus.ACTIVE).scalar()
            or 0
        )

    def get_active_count_in_range(self, db_session: DbSession, start_date: datetime, end_date: datetime) -> int:
        """Get count of active connections created within a date range."""
        return (
            db_session.query(func.count(self.model.id))
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.created_at >= start_date,
                    self.model.created_at < end_date,
                ),
            )
            .scalar()
            or 0
        )

    def get_users_with_active_conn_count(self, db_session: DbSession) -> int:
        """Count of distinct users with at least one active connection."""
        return (
            db_session.query(func.count(func.distinct(self.model.user_id)))
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .scalar()
            or 0
        )

    def get_users_with_multi_active_conn_count(self, db_session: DbSession) -> int:
        """Count of distinct users with more than one active connection."""
        subq = (
            select(self.model.user_id)
            .where(self.model.status == ConnectionStatus.ACTIVE)
            .group_by(self.model.user_id)
            .having(func.count(self.model.id) > 1)
            .subquery()
        )
        return db_session.query(func.count()).select_from(subq).scalar() or 0

    def get_top_providers_by_active_conn(self, db_session: DbSession, limit: int = 3) -> list[tuple[str, int]]:
        """Top providers by active connection count, returns (provider, count) pairs."""
        rows = (
            db_session.query(self.model.provider, func.count(self.model.id).label("cnt"))
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .group_by(self.model.provider)
            .order_by(func.count(self.model.id).desc())
            .limit(limit)
            .all()
        )
        return [(row.provider, row.cnt) for row in rows]

    def get_by_user_and_provider(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> UserConnection | None:
        """The member's PRIMARY connection for a provider: active before revoked, then oldest.

        **Twelve of the thirteen providers only ever have one row, and for them this is what it
        has always been.** Withings can have several — a member may link their own account and
        also be shipped cellular devices, each of which creates an account we provision — so this
        needs a stated rule rather than an accident of query planning. Seventeen call sites
        depend on the answer.

        The rule: ACTIVE before revoked, then ``created_at`` ascending, then ``id``. It is
        deliberately the same ordering ``_active_by_provider_external_id`` documents a few methods
        down, so the repository carries ONE notion of "primary" rather than two.

        Two properties are load-bearing:

        * **Ordered, not filtered.** Revoked rows are ranked last but still returned, because
          ``base_oauth._save_connection`` looks a connection up in order to REACTIVATE it. Filter
          them out and that path stops finding the row it means to revive and creates a duplicate
          instead.
        * **``.first()``, not ``.one_or_none()``.** This used to end in ``one_or_none``, which
          raises ``MultipleResultsFound`` the moment a member has two connections for a provider —
          i.e. every Withings call site would have started throwing when the unique index was
          relaxed.

        Callers that must act on a SPECIFIC connection rather than the primary one should not use
        this at all: pass a ``connection_id`` (the token and data paths take one) or ask
        ``withings.connections`` which connections a member has.
        """
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.user_id == user_id,
                    self.model.provider == provider,
                ),
            )
            .order_by(
                case((self.model.status == ConnectionStatus.ACTIVE, 0), else_=1),
                self.model.created_at.asc(),
                self.model.id.asc(),
            )
            .first()
        )

    def get_active_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> UserConnection | None:
        """The member's primary ACTIVE connection for a provider — see ``get_by_user_and_provider``.

        Same rule minus the status term, which the filter has already settled: ordering by status
        here would be a no-op implying a distinction this query cannot make.
        """
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.user_id == user_id,
                    self.model.provider == provider,
                    self.model.status == ConnectionStatus.ACTIVE,
                ),
            )
            .order_by(self.model.created_at.asc(), self.model.id.asc())
            .first()
        )

    def get_by_user_provider_and_account(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        provider_user_id: str | None,
    ) -> UserConnection | None:
        """The member's connection for ONE SPECIFIC provider account, whatever its status.

        This is the OAuth callback's question — "have I seen this exact account for this member
        before?" — and it is the natural key of ``ix_user_connection_user_provider``, so at most
        one row can ever match.

        Asking ``get_by_user_and_provider`` instead is a live hazard once a member can hold two
        Withings accounts: that returns the member's PRIMARY connection, which may well be the
        device account we provisioned, and the callback would then overwrite its tokens with the
        personal account's. That is the same destructive collapse the provisioning overwrite is
        being deleted for, arriving from the other direction.

        A ``None`` ``provider_user_id`` falls back to the primary lookup. Providers that report no
        account id can only ever have one row for a member — the unique index is NULLS NOT
        DISTINCT — so the two questions coincide there, and today's behaviour is preserved.
        """
        if provider_user_id is None:
            return self.get_by_user_and_provider(db_session, user_id, provider)
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.user_id == user_id,
                    self.model.provider == provider,
                    self.model.provider_user_id == provider_user_id,
                ),
            )
            .one_or_none()
        )

    def _active_by_provider_external_id(
        self, db_session: DbSession, provider: str, provider_user_id: str
    ) -> Query[UserConnection]:
        """Base query: active connections for a given (provider, provider_user_id) pair.

        Ordered by created_at asc, id asc so the oldest connection is always
        index 0 — stable primary attribution in webhook fan-out across query
        plans and restarts.
        """
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.provider == provider,
                    self.model.provider_user_id == provider_user_id,
                    self.model.status == ConnectionStatus.ACTIVE,
                )
            )
            .order_by(self.model.created_at.asc(), self.model.id.asc())
        )

    def get_all_by_provider_user_id(
        self,
        db_session: DbSession,
        provider: str,
        provider_user_id: str,
    ) -> list[UserConnection]:
        """Get all active connections sharing the same external provider account.

        Used for multi-account sync fan-out: one provider account connected to
        several OpenWearables profiles.
        """
        return self._active_by_provider_external_id(db_session, provider, provider_user_id).all()

    def get_by_provider_user_id(
        self,
        db_session: DbSession,
        provider: str,
        provider_user_id: str,
    ) -> UserConnection | None:
        """Get connection by provider and provider's user ID.

        Useful for webhook processing where we receive provider's user ID
        and need to find our internal user.
        """
        try:
            return self._active_by_provider_external_id(db_session, provider, provider_user_id).one_or_none()
        except MultipleResultsFound:
            logger.warning(
                "Multiple active connections found for provider_user_id — returning first",
                extra={"provider": provider, "provider_user_id": provider_user_id},
            )
            return self._active_by_provider_external_id(db_session, provider, provider_user_id).first()

    def get_by_provider_username(
        self,
        db_session: DbSession,
        provider: str,
        provider_username: str,
    ) -> UserConnection | None:
        """Get connection by provider and provider's display username.

        Used by Suunto webhooks — the ``username`` field in the payload matches
        the ``user`` JWT claim stored as ``provider_username``.
        """
        try:
            return (
                db_session.query(self.model)
                .filter(
                    and_(
                        self.model.provider == provider,
                        self.model.provider_username == provider_username,
                        self.model.status == ConnectionStatus.ACTIVE,
                    ),
                )
                .one_or_none()
            )
        except MultipleResultsFound:
            logger.warning(
                "Multiple active connections found for provider_username — returning first",
                extra={"provider": provider, "provider_username": provider_username},
            )
            return (
                db_session.query(self.model)
                .filter(
                    and_(
                        self.model.provider == provider,
                        self.model.provider_username == provider_username,
                        self.model.status == ConnectionStatus.ACTIVE,
                    ),
                )
                .first()
            )

    def get_linked_user_ids(
        self,
        db_session: DbSession,
        exclude_user_id: UUID,
        provider_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[UUID]]:
        """For a list of (provider, provider_user_id) pairs, return other active OW users
        sharing the same external account, grouped by pair."""
        if not provider_pairs:
            return {}
        rows = (
            db_session.query(self.model.provider, self.model.provider_user_id, self.model.user_id)
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.user_id != exclude_user_id,
                    tuple_(self.model.provider, self.model.provider_user_id).in_(provider_pairs),
                )
            )
            .all()
        )
        result: dict[tuple[str, str], list[UUID]] = {}
        for provider, provider_user_id, linked_user_id in rows:
            result.setdefault((provider, provider_user_id), []).append(linked_user_id)
        return result

    def get_by_user_id(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[UserConnection]:
        """Get all connections for a specific user."""
        return (
            db_session.query(self.model)
            .filter(self.model.user_id == user_id)
            .order_by(self.model.created_at.desc())
            .all()
        )

    def get_expiring_tokens(self, db_session: DbSession, minutes_threshold: int = 5) -> list[UserConnection]:
        """Get connections with tokens expiring soon (for background refresh)."""
        now = datetime.now(timezone.utc)

        threshold_time = now + timedelta(minutes=minutes_threshold)

        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.token_expires_at <= threshold_time,
                ),
            )
            .all()
        )

    def disconnect(self, db_session: DbSession, user_id: UUID, provider: str) -> int:
        """Revoke EVERY connection a member has with a provider, in one UPDATE.

        "Disconnect Withings" in its widest sense. For a member who holds several Withings
        accounts — their own, plus one per cellular device we shipped — this revokes all of
        them; use ``disconnect_connection`` to remove just one.
        """
        result = cast(
            CursorResult[tuple[()]],
            db_session.execute(
                update(UserConnection)
                .where(
                    and_(
                        UserConnection.user_id == user_id,
                        UserConnection.provider == provider,
                        UserConnection.status != ConnectionStatus.REVOKED,
                    ),
                )
                .values(
                    status=ConnectionStatus.REVOKED,
                    access_token=None,
                    refresh_token=None,
                    token_expires_at=None,
                    updated_at=datetime.now(timezone.utc),
                ),
            ),
        )
        db_session.commit()
        return result.rowcount

    def disconnect_connection(self, db_session: DbSession, connection: UserConnection) -> int:
        """Revoke ONE connection and clear its tokens. Returns rows updated (0 or 1).

        The sibling ``disconnect`` above revokes every connection a member has with a provider,
        which is right for "disconnect Withings entirely" and wrong for "remove this device":
        a member's own linked account and an account we created to ship them hardware are
        separately revocable things, and collapsing them is the destructive move this whole
        change exists to remove.
        """
        result = cast(
            CursorResult[tuple[()]],
            db_session.execute(
                update(UserConnection)
                .where(
                    and_(
                        UserConnection.id == connection.id,
                        UserConnection.status != ConnectionStatus.REVOKED,
                    ),
                )
                .values(
                    status=ConnectionStatus.REVOKED,
                    access_token=None,
                    refresh_token=None,
                    token_expires_at=None,
                    updated_at=datetime.now(timezone.utc),
                ),
            ),
        )
        db_session.commit()
        return result.rowcount

    def mark_as_revoked(self, db_session: DbSession, connection: UserConnection) -> UserConnection:
        """Mark connection as revoked (when refresh token fails)."""
        connection.status = ConnectionStatus.REVOKED
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_scope(self, db_session: DbSession, connection: UserConnection, scope: str | None) -> UserConnection:
        """Update connection scope (e.g. when user changes permissions on Garmin Connect)."""
        connection.scope = scope
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_tokens(
        self,
        db_session: DbSession,
        connection: UserConnection,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
    ) -> UserConnection:
        """Update connection with new tokens after refresh."""

        connection.access_token = access_token
        if refresh_token:
            connection.refresh_token = refresh_token
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_connection_info(
        self,
        db_session: DbSession,
        connection: UserConnection,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
        provider_user_id: str | None = None,
        provider_username: str | None = None,
        scope: str | None = None,
    ) -> UserConnection:
        """Update connection with new tokens and user info."""
        connection.access_token = access_token
        if refresh_token:
            connection.refresh_token = refresh_token
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        if provider_user_id and not connection.provider_user_id:
            connection.provider_user_id = provider_user_id
        if provider_username and not connection.provider_username:
            connection.provider_username = provider_username
        if scope and connection.scope != scope:
            connection.scope = scope

        connection.status = ConnectionStatus.ACTIVE
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_last_synced_at(self, db_session: DbSession, connection: UserConnection) -> UserConnection:
        """Update the last synced timestamp."""
        connection.last_synced_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def get_all_active_by_user(self, db_session: DbSession, user_id: UUID) -> list[UserConnection]:
        """Get all active connections for a specific user."""
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.user_id == user_id,
                    self.model.status == ConnectionStatus.ACTIVE,
                ),
            )
            .all()
        )

    def get_all_active_by_provider(self, db_session: DbSession, provider: str) -> list[UserConnection]:
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.provider == provider,
                    self.model.status == ConnectionStatus.ACTIVE,
                ),
            )
            .all()
        )

    def get_all_active_users(self, db_session: DbSession) -> list[UUID]:
        """Get all unique user IDs that have active connections."""
        return [
            row.user_id
            for row in db_session.query(self.model.user_id)
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .distinct()
            .all()
        ]

    def ensure_sdk_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> tuple[UserConnection, SdkConnectionOutcome]:
        """Ensure an SDK-based connection exists for a user and provider.

        SDK-based providers (like Apple Health) don't use OAuth tokens.
        This method creates or returns an existing connection without tokens.

        Returns the connection and which branch was taken, so the caller can emit
        ``connection.created`` only on a real state change. The upload path calls
        this on every batch, so EXISTING must stay silent.
        """
        existing = self.get_by_user_and_provider(db_session, user_id, provider)
        if existing:
            # Reactivate if revoked
            if existing.status != ConnectionStatus.ACTIVE:
                existing.status = ConnectionStatus.ACTIVE
                existing.updated_at = datetime.now(timezone.utc)
                db_session.add(existing)
                db_session.commit()
                db_session.refresh(existing)
                return existing, SdkConnectionOutcome.REACTIVATED
            return existing, SdkConnectionOutcome.EXISTING

        # Create new SDK connection (no tokens needed)
        connection = UserConnection(
            id=uuid4(),
            user_id=user_id,
            provider=provider,
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            status=ConnectionStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection, SdkConnectionOutcome.CREATED
