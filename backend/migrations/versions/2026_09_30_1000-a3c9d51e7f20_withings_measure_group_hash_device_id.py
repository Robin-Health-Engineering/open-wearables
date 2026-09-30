"""withings measure group hash_device_id

Revision ID: a3c9d51e7f20
Revises: e5b8f3c02d46

A measure group's own hash_deviceid. The cellular Body Pro 2 sends an integer deviceid on its
groups that Getdevice never lists; only the hash joins such a group to its withings_device row,
which the readings API is asked for by. Nullable, no backfill: nothing from those devices was
ever recorded (every group was rejected as unparseable), and other devices keep matching on
device_id. Reversible: downgrade drops the column.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9d51e7f20"
down_revision: Union[str, None] = "e5b8f3c02d46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("withings_measure_group", sa.Column("hash_device_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("withings_measure_group", "hash_device_id")
