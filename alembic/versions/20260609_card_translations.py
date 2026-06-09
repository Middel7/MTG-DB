"""Ajout table card_translations — noms traduits des cartes

Revision ID: 20260609_card_translations
Revises: 20260607_cardmarket_tables
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "20260609_card_translations"
down_revision = "20260607_cardmarket_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "card_translations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.Integer(), sa.ForeignKey("cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lang", sa.String(10), nullable=False),
        sa.Column("printed_name", sa.Text(), nullable=False),
        sa.UniqueConstraint("card_id", "lang", name="uq_card_translations_card_lang"),
    )
    op.create_index("ix_card_translations_card_id", "card_translations", ["card_id"])
    op.create_index("ix_card_translations_lang", "card_translations", ["lang"])
    op.create_index("ix_card_translations_printed_name", "card_translations", ["printed_name"])


def downgrade() -> None:
    op.drop_index("ix_card_translations_printed_name", table_name="card_translations")
    op.drop_index("ix_card_translations_lang", table_name="card_translations")
    op.drop_index("ix_card_translations_card_id", table_name="card_translations")
    op.drop_table("card_translations")
