"""withings sdk account

Revision ID: c7e1a49b83d2
Revises: dc5ac28c4b94

Per-member state for the Withings Mobile SDK (phase 2): the csrf_token the hosted
SDK WebViews require, and the external_id we minted at createuser.

A table of its own rather than columns on user_connection. That table is upstream's
and this fork has to keep rebasing onto upstream cleanly, so widening it invites a
conflict on every rebase; and none of this means anything to the other twelve
providers. Device advertise_keys land here next.

The FK is NOT NULL with ON DELETE CASCADE, unlike the shared FKUserConnection alias
(nullable, SET NULL): a csrf_token belongs to one token pair, so an orphaned row is
never a state worth keeping.

Both timestamps are timestamptz, matching what the model declares: BaseDbModel's
type_annotation_map maps datetime to DateTime(timezone=True), so a naive column here
would silently discard the offset on every write.

Reversible: downgrade drops the table. Nothing else references it, and the data is
recoverable — a csrf_token is reissued on every token refresh.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1a49b83d2"
down_revision: Union[str, None] = "dc5ac28c4b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "withings_sdk_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_connection_id"], ["user_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )
    # One SDK account per connection. Two rows would mean two csrf_tokens for one token
    # pair, with nothing to say which is current.
    op.create_index(
        "ix_withings_sdk_account_connection",
        "withings_sdk_account",
        ["user_connection_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_withings_sdk_account_connection", table_name="withings_sdk_account")
    op.drop_table("withings_sdk_account")
