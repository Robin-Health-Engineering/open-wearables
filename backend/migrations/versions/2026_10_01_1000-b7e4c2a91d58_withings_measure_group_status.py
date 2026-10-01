"""withings measure group status and raw

Revision ID: b7e4c2a91d58
Revises: a3c9d51e7f20

Spec 2026-10-01 (purchased-scale attribution). ``status`` says whether a session is the member's:
registered (every existing row, via the server default), pending (an ambiguous attrib 1 weigh-in
awaiting the member's answer) or discarded. ``raw`` holds a pending group's Withings payload so
confirming needs no Withings call; NULL otherwise.

Reversible: downgrade drops the partial index, the check constraint and both columns. Rows are
kept, so after a downgrade a pending or discarded group reads as a registered group with no
samples (staging-only feature; acceptable).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e4c2a91d58"
down_revision: Union[str, None] = "a3c9d51e7f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "withings_measure_group",
        sa.Column("status", sa.String(length=16), server_default="registered", nullable=False),
    )
    op.add_column(
        "withings_measure_group",
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_withings_measure_group_status",
        "withings_measure_group",
        "status IN ('registered', 'pending', 'discarded')",
    )
    op.create_index(
        "ix_withings_measure_group_pending_measured_at",
        "withings_measure_group",
        ["measured_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_withings_measure_group_pending_measured_at", table_name="withings_measure_group")
    op.drop_constraint("ck_withings_measure_group_status", "withings_measure_group", type_="check")
    op.drop_column("withings_measure_group", "raw")
    op.drop_column("withings_measure_group", "status")
