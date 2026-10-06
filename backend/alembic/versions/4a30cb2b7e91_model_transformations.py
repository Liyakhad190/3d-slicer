"""Persist active model transformations.

Revision ID: 4a30cb2b7e91
Revises: 36115a9e1f9f
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import JSONType, UUIDType

revision: str = "4a30cb2b7e91"
down_revision: str | None = "36115a9e1f9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("transformed_blob_id", UUIDType(), nullable=True))
        batch_op.add_column(sa.Column("transformation", JSONType(), nullable=True))
        batch_op.create_foreign_key(
            "fk_model_versions_transformed_blob_id_blobs",
            "blobs",
            ["transformed_blob_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("slice_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model_input_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("slice_jobs", schema=None) as batch_op:
        batch_op.drop_column("model_input_sha256")
    with op.batch_alter_table("model_versions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_model_versions_transformed_blob_id_blobs", type_="foreignkey"
        )
        batch_op.drop_column("transformation")
        batch_op.drop_column("transformed_blob_id")