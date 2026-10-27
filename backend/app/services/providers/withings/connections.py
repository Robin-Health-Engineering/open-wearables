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

**Sold devices (spec 2026-10-27 O2).** A device Robin sold is not always on an account we created:
the non-cellular Body Comp is set up on the member's OWN account. "Is this weigh-in from the scale we
sold?" is therefore answered per device, by ``RobinDeviceScope``: every device on a provisioned
connection, and on a self-linked connection only the ``withings_device`` rows whose ``order_ref`` is
set (robin-backend tags them after matching ``hash_deviceid`` to the order). Reading events, the
attrib-1 hold and the readings API use ``robin_device_scope(s)`` / ``robin_groups_clause``; the
provisioned-only helpers above stay for what is genuinely cellular-only (``endpartnerprogram``, the
SDK session and recovery) and must not be used to answer the sold-device question.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select

from app.database import DbSession
from app.models import User, UserConnection, WithingsMeasureGroupRecord
from app.models.withings_device import WithingsDevice
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

    Answers for a connection in any state, unlike the active-only helpers above — a disconnect resolves
    the connection first and asks this second, by which point it may already be revoked.
    """
    return bool(_provisioned_connection_ids(db, [connection_id]))


def all_device_connection_ids(db: DbSession, user_id: UUID) -> list[UUID]:
    """Ids of the accounts we provisioned for this member, in any state (active, revoked, inactive).

    For callers that must still recognise a weigh-in after its account was disconnected, such as a
    repeated discard (contract A2b.1).
    """
    ids = [
        row[0]
        for row in db.query(UserConnection.id)
        .filter(UserConnection.user_id == user_id, UserConnection.provider == ProviderName.WITHINGS.value)
        .all()
    ]
    provisioned = _provisioned_connection_ids(db, ids)
    return [connection_id for connection_id in ids if connection_id in provisioned]


@dataclass(frozen=True)
class RobinDeviceScope:
    """Which weigh-ins on ONE Withings connection come from a device Robin sold the member.

    ``provisioned``: an account we created to ship a device — every device on it is ours.
    Otherwise the member's own account, and only sessions naming one of ``sold_keys`` (the
    ``device_id`` and ``hash_device_id`` of its ``withings_device`` rows with ``order_ref`` set).
    A group is matched by its ``hash_deviceid`` OR its ``deviceid``: the cellular Body Pro 2 sends a
    deviceid Getdevice never lists, and older groups carry no hash.
    """

    connection_id: UUID
    provisioned: bool
    sold_keys: frozenset[str] = frozenset()

    def covers(self, hash_device_id: str | None, device_id: str | None) -> bool:
        if self.provisioned:
            return True
        return (hash_device_id is not None and hash_device_id in self.sold_keys) or (
            device_id is not None and device_id in self.sold_keys
        )


def _sold_keys(db: DbSession, connection_ids: list[UUID]) -> dict[UUID, frozenset[str]]:
    """Per connection, the ids of its devices Robin sold; connections with none are absent."""
    if not connection_ids:
        return {}
    rows = db.query(WithingsDevice.user_connection_id, WithingsDevice.device_id, WithingsDevice.hash_device_id).filter(
        WithingsDevice.user_connection_id.in_(connection_ids), WithingsDevice.order_ref.is_not(None)
    )
    keys: dict[UUID, set[str]] = {}
    for connection_id, device_id, hash_device_id in rows:
        keys.setdefault(connection_id, set()).update(key for key in (device_id, hash_device_id) if key)
    return {connection_id: frozenset(values) for connection_id, values in keys.items()}


def _scopes(db: DbSession, connection_ids: list[UUID]) -> list[RobinDeviceScope]:
    provisioned = _provisioned_connection_ids(db, connection_ids)
    sold = _sold_keys(db, [c for c in connection_ids if c not in provisioned])
    scopes: list[RobinDeviceScope] = []
    for connection_id in connection_ids:
        if connection_id in provisioned:
            scopes.append(RobinDeviceScope(connection_id=connection_id, provisioned=True))
        elif connection_id in sold:
            scopes.append(
                RobinDeviceScope(connection_id=connection_id, provisioned=False, sold_keys=sold[connection_id])
            )
    return scopes


def robin_device_scope(db: DbSession, connection_id: UUID) -> RobinDeviceScope | None:
    """This connection's scope, in any connection state; None when nothing on it is a device Robin sold."""
    scopes = _scopes(db, [connection_id])
    return scopes[0] if scopes else None


def robin_device_scopes(db: DbSession, user_id: UUID, *, any_state: bool = False) -> list[RobinDeviceScope]:
    """The member's connections holding a device Robin sold, oldest first. Active ones only, unless ``any_state``.

    ``any_state`` keeps a revoked or inactive connection, so a repeated discard still finds the
    tombstone of a disconnected account (contract A2b.1).
    """
    if any_state:
        ids = [
            row[0]
            for row in db.query(UserConnection.id)
            .filter(UserConnection.user_id == user_id, UserConnection.provider == ProviderName.WITHINGS.value)
            .order_by(UserConnection.created_at.asc(), UserConnection.id.asc())
            .all()
        ]
    else:
        ids = [c.id for c in active_withings_connections(db, user_id)]
    return _scopes(db, ids)


def robin_groups_clause(scopes: list[RobinDeviceScope]) -> ColumnElement[bool] | None:
    """SQL over ``withings_measure_group``: the groups these scopes cover. None when no scope."""
    if not scopes:
        return None
    group = WithingsMeasureGroupRecord
    clauses: list[ColumnElement[bool]] = []
    provisioned = [s.connection_id for s in scopes if s.provisioned]
    if provisioned:
        clauses.append(group.user_connection_id.in_(provisioned))
    for scope in scopes:
        if not scope.provisioned:
            keys = sorted(scope.sold_keys)
            clauses.append(
                and_(
                    group.user_connection_id == scope.connection_id,
                    or_(group.hash_device_id.in_(keys), group.device_id.in_(keys)),
                )
            )
    return or_(*clauses)


def robin_external_user_id(db: DbSession, connection: UserConnection) -> str | None:
    """Who a reading event on this connection is for (contract C3): None means send nothing.

    A provisioned connection: its ``withings_sdk_account.external_id`` (unchanged). The member's own
    account: ``User.external_user_id``, the Robin CustomerProfile id ``ensureOwUser`` sets.
    """
    external_id = db.execute(
        select(WithingsSdkAccount.external_id).where(WithingsSdkAccount.user_connection_id == connection.id)
    ).scalar_one_or_none()
    if external_id is not None:
        return external_id
    return db.execute(select(User.external_user_id).where(User.id == connection.user_id)).scalar_one_or_none() or None
