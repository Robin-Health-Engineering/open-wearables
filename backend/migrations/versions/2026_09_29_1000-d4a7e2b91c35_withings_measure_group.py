"""withings measure group

Revision ID: d4a7e2b91c35
Revises: b8d42f07ce91

One row per Withings measurement group per connection, with the capturing deviceid. It exists so
OW can say (a) which of a member's accounts and devices took a reading and (b) whether a group is
new. (b) is what the Robin reading event is sent for.

Starts empty on purpose. Withings is the only source of deviceid, and the next sync of each
connection fills its recent window. The event sender's max-age guard is what stops that first
fill from looking like a burst of new readings.

Reversible: downgrade drops the table; nothing references it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7e2b91c35"
down_revision: Union[str, None] = "b8d42f07ce91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "withings_measure_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("user_connection_id", sa.Uuid(), nullable=False),
        sa.Column("grpid", sa.String(length=32), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("attrib", sa.Integer(), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_connection_id"], ["user_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_withings_measure_group_connection_grpid",
        "withings_measure_group",
        ["user_connection_id", "grpid"],
        unique=True,
    )
    op.create_index(
        "ix_withings_measure_group_device_time",
        "withings_measure_group",
        ["user_connection_id", "device_id", "measured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_withings_measure_group_device_time", table_name="withings_measure_group")
    op.drop_index("uq_withings_measure_group_connection_grpid", table_name="withings_measure_group")
    op.drop_table("withings_measure_group")
