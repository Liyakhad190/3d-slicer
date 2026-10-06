"""Add validation_reason and provenance to machine_profiles.

Revision ID: c7e2b149a8d3
Revises: 8d4c1b6a3f20
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import CitextType, JSONType, TZDateTime, UUIDType

revision: str = "c7e2b149a8d3"
down_revision: str | None = "8d4c1b6a3f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("machine_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("validation_reason", sa.String(500), nullable=True))
        batch_op.add_column(sa.Column("provenance", sa.String(40), server_default="seed", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("machine_profiles", schema=None) as batch_op:
        batch_op.drop_column("provenance")
        batch_op.drop_column("validation_reason")
