"""withings device

Revision ID: f3a91c62d70e
Revises: c7e1a49b83d2

Per-device state for the Withings Mobile SDK (phase 2), and it exists for one column:
advertise_key. Background BLE sync cannot start without that per-device token, so a
device row without one is a device that quietly stops reporting between Wi-Fi sessions.

Withings hands the key out two ways and says both must be implemented — the SDK's
install-success notification and User v2 - Getdevice — hence two writers into one row,
keyed by the unique (user_connection_id, device_id) index, and advertise_key_source
recording which of them last supplied the value.

Separate from withings_sdk_account because the cardinality differs: one SDK account,
many devices. Separate from user_connection for that table's own reason — it is
upstream's, and this fork has to keep rebasing onto it cleanly.

dissociated_at is a soft marker rather than a delete. Neither source is complete, and a
Getdevice response that transiently omits a device would otherwise destroy an
advertise_key that only the install notification ever carried and that nothing can
re-derive.

last_getdevice_at records when a Getdevice response last listed a device, and is what
makes that sweep safe: a device Getdevice has NEVER listed says nothing by being absent
from it — it may simply be newer than Getdevice's view — so only devices it has listed
before are candidates for dissociation.

Reversible: downgrade drops the table. Nothing references it, and both writers can
repopulate it — the install notification on the next setup, Getdevice on demand.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a91c62d70e"
down_revision: Union[str, None] = "c7e1a49b83d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "withings_device",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_connection_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("device_type", sa.String(length=32), nullable=True),
        sa.Column("advertise_key", sa.String(length=255), nullable=True),
        sa.Column("advertise_key_source", sa.String(length=32), nullable=True),
        sa.Column("last_session_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_getdevice_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dissociated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_connection_id"], ["user_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The upsert key for both writers. Without it, a notification and a Getdevice sync
    # reporting the same physical device produce two rows, and nothing says which
    # advertise_key is current.
    op.create_index(
        "ix_withings_device_connection_device",
        "withings_device",
        ["user_connection_id", "device_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_withings_device_connection_device", table_name="withings_device")
    op.drop_table("withings_device")
