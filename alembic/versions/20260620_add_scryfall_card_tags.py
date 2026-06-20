"""Ajoute la table scryfall_card_tags (tags oracle depuis Scryfall Tagger)

Revision ID: 20260620_add_scryfall_card_tags
Revises: 20260609_rename_scryfall_tables
Create Date: 2026-06-20
"""
import sqlalchemy as sa
from alembic import op

revision = "20260620_add_scryfall_card_tags"
down_revision = "20260609_rename_scryfall_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scryfall_card_tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("tag_name", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["card_id"], ["scryfall_cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_id", "tag_name", name="uq_card_tag"),
    )
    op.create_index("ix_scryfall_card_tags_card_id", "scryfall_card_tags", ["card_id"])
    op.create_index("ix_scryfall_card_tags_tag_name", "scryfall_card_tags", ["tag_name"])


def downgrade() -> None:
    op.drop_index("ix_scryfall_card_tags_tag_name", table_name="scryfall_card_tags")
    op.drop_index("ix_scryfall_card_tags_card_id", table_name="scryfall_card_tags")
    op.drop_table("scryfall_card_tags")
