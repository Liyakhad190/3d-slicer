"""Archive printers while preserving job history.

Revision ID: 8d4c1b6a3f20
Revises: 4a30cb2b7e91
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import TZDateTime

revision: str = "8d4c1b6a3f20"
down_revision: str | None = "4a30cb2b7e91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("printers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archived_at", TZDateTime(), nullable=True))
        batch_op.create_index("ix_printers_archived_at", ["archived_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("printers", schema=None) as batch_op:
        batch_op.drop_index("ix_printers_archived_at")
        batch_op.drop_column("archived_at")