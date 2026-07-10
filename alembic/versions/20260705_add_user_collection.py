"""Ajoute la table user_collection

Revision ID: 20260705_add_user_collection
Revises: 20260620_add_scryfall_card_tags
Create Date: 2026-07-05
"""
import sqlalchemy as sa
from alembic import op

revision = "20260705_add_user_collection"
down_revision = "20260620_add_scryfall_card_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_collection",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("raw_line", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_collection_card_name", "user_collection", ["card_name"])


def downgrade() -> None:
    op.drop_index("ix_user_collection_card_name", table_name="user_collection")
    op.drop_table("user_collection")
