"""withings device hash_device_id

Revision ID: e5b8f3c02d46
Revises: d4a7e2b91c35

Withings' hash_deviceid, from Getdevice. It is the key the dropshipment order detail also carries,
and so the link from a device to the order it shipped on. Nullable, no backfill: the next
Getdevice sync fills it. Reversible: downgrade drops the column.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b8f3c02d46"
down_revision: Union[str, None] = "d4a7e2b91c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("withings_device", sa.Column("hash_device_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("withings_device", "hash_device_id")
