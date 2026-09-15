"""Tell a member's own Withings account from one we provisioned for a device.

A Robin member can hold several Withings connections at once, and they are not the same kind
of thing:

* **member-linked** — they signed into their own Withings account through consumer OAuth. Their
  account, their password. Disconnecting means we stop reading it, and nothing else.
* **device-provisioned** — we created the account, through the partner API, in order to ship
  them a cellular device. It sits on a cellular plan we are billed for, so disconnecting has to
  tell Withings as well (``end_program``).

There is no ``kind`` column, and deliberately so. ``user_connection`` is upstream's table and
every column this fork adds to it is a permanent rebase surface, for a distinction the other
twelve providers will never make. The classification already exists in the schema instead:
``provision_sdk_account`` always writes a ``withings_sdk_account`` row, and a consumer-OAuth
callback never does. Row present means we created the account; row absent means the member did.

These helpers are the only place that inference lives. Do not re-derive it at a call site — if
the discriminator ever changes, it should change once.
"""

from uuid import UUID

from sqlalchemy import select

from app.database import DbSession
from app.models import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName


def _provisioned_connection_ids(db: DbSession, connection_ids: list[UUID]) -> set[UUID]:
    """Which of these connection ids have a ``withings_sdk_account`` row."""
    if not connection_ids:
        return set()
    rows = db.execute(
        select(WithingsSdkAccount.user_connection_id).where(WithingsSdkAccount.user_connection_id.in_(connection_ids))
    ).all()
    return {row[0] for row in rows}


def active_withings_connections(db: DbSession, user_id: UUID) -> list[UserConnection]:
    """Every active Withings connection for a member, oldest first.

    Oldest first for the same reason the repository orders that way: it is the fork's one notion
    of "primary", and a member's own account almost always predates a device we shipped them.
    """
    return list(
        db.query(UserConnection)
        .filter(
            UserConnection.user_id == user_id,
            UserConnection.provider == ProviderName.WITHINGS.value,
            UserConnection.status == ConnectionStatus.ACTIVE,
        )
        .order_by(UserConnection.created_at.asc(), UserConnection.id.asc())
        .all()
    )


def member_linked_connection(db: DbSession, user_id: UUID) -> UserConnection | None:
    """The account the MEMBER owns, if they have linked one.

    At most one: consumer OAuth writes a single connection per Withings account, and a member
    signing in twice re-links the same one.
    """
    connections = active_withings_connections(db, user_id)
    provisioned = _provisioned_connection_ids(db, [c.id for c in connections])
    for connection in connections:
        if connection.id not in provisioned:
            return connection
    return None


def device_connections(db: DbSession, user_id: UUID) -> list[UserConnection]:
    """The accounts WE created in order to ship this member a device, oldest first.

    A list, not an optional. A member holds at most one provisioned account today — Withings
    reuse the one createuserorder made on their later orders — but the shape is what stops a
    caller writing ``the`` provisioned connection: historic members can hold more than one, and
    what matters here is the personal-versus-provisioned split, not the count.
    """
    connections = active_withings_connections(db, user_id)
    provisioned = _provisioned_connection_ids(db, [c.id for c in connections])
    return [c for c in connections if c.id in provisioned]


def is_device_connection(db: DbSession, connection_id: UUID) -> bool:
    """Whether this connection is one we provisioned, rather than the member's own.

    Answers for a connection in any state, unlike the two helpers above — a disconnect resolves
    the connection first and asks this second, by which point it may already be revoked.
    """
    return bool(_provisioned_connection_ids(db, [connection_id]))
