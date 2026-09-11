"""withings multi account

Revision ID: b8d42f07ce91
Revises: f3a91c62d70e

Three changes, all of them consequences of one fact: a Withings cellular device cannot be
activated onto an account the partner did not create. So a member who has linked their own
Withings account and is then shipped a device holds TWO Withings accounts, and another for
every later order.

1. ``ix_user_connection_user_provider`` gains ``provider_user_id``.

   It stays unique — two rows for the same provider account on one member is still a bug worth
   a constraint, and it is the guard if Withings ever adopts an existing account instead of
   creating one. Recreated under the SAME NAME so the diff against upstream stays one index
   rather than one removed and one added.

   NULLS NOT DISTINCT is the load-bearing half. ``provider_user_id`` is nullable and SDK-based
   providers (Apple) never set one; Postgres treats NULLs as distinct in a unique index, so
   without the clause those connections would lose their uniqueness guarantee entirely — a
   provider with nothing to do with Withings, broken by a Withings change. PG15+; we run 18.

2. ``withings_device`` is repurposed.

   It was created for ``advertise_key``, the per-device token the Withings Mobile SDK needs to
   start background BLE sync. That SDK integration is abandoned — cellular devices ship already
   connected, so there is no pairing to bridge — and the column has no reader left. What the
   table is FOR now is the device information a member is shown: which device, when it last
   synced, how its battery is doing.

   ``battery`` is added because Getdevice already returns it and we were parsing it into
   nothing. ``order_ref`` links a device back to the robin-backend order that shipped it;
   nullable, because a device the member owned before we shipped them anything has no order.

   This table is merged but has never been deployed — staging still runs the image built before
   it — so no row is being migrated here, only the shape.

3. ``withings_sdk_account.external_id`` widens from 64 to 128.

   robin-backend currently sends the bare CustomerProfile id, which is unique per member and
   therefore collides the moment a member has a second partner-created account. Its replacement
   is ``{customerProfileId}#{orderRef}`` — two UUIDs and a separator, 73 characters. Widening
   here must land BEFORE robin-backend starts sending the longer value.

Reversible, with one caveat stated plainly: ``downgrade`` restores the two-column unique index,
which will FAIL if any member has by then accumulated a second connection for one provider. That
is correct — silently dropping one of a member's two Withings accounts to satisfy an index would
lose data. Resolve the duplicates first if a downgrade is ever genuinely needed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d42f07ce91"
down_revision: Union[str, None] = "f3a91c62d70e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_user_connection_user_provider", table_name="user_connection")
    op.create_index(
        "ix_user_connection_user_provider",
        "user_connection",
        ["user_id", "provider", "provider_user_id"],
        unique=True,
    )

    op.add_column("withings_device", sa.Column("battery", sa.String(length=32), nullable=True))
    op.add_column("withings_device", sa.Column("order_ref", sa.String(length=64), nullable=True))
    op.drop_column("withings_device", "advertise_key")
    op.drop_column("withings_device", "advertise_key_source")

    op.alter_column(
        "withings_sdk_account",
        "external_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "withings_sdk_account",
        "external_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )

    op.add_column("withings_device", sa.Column("advertise_key_source", sa.String(length=32), nullable=True))
    op.add_column("withings_device", sa.Column("advertise_key", sa.String(length=255), nullable=True))
    op.drop_column("withings_device", "order_ref")
    op.drop_column("withings_device", "battery")

    # Fails if a member has accumulated two connections for one provider. Deliberately — see the
    # module docstring.
    op.drop_index("ix_user_connection_user_provider", table_name="user_connection")
    op.create_index(
        "ix_user_connection_user_provider",
        "user_connection",
        ["user_id", "provider"],
        unique=True,
    )
