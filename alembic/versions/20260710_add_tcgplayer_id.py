"""Ajoute tcgplayer_id à scryfall_card_printings

Revision ID: 20260710_add_tcgplayer_id
Revises: 20260620_add_scryfall_card_tags
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op

revision = "20260710_add_tcgplayer_id"
down_revision = "20260705_add_user_collection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scryfall_card_printings",
        sa.Column("tcgplayer_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_scryfall_card_printings_tcgplayer_id",
        "scryfall_card_printings",
        ["tcgplayer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scryfall_card_printings_tcgplayer_id",
        table_name="scryfall_card_printings",
    )
    op.drop_column("scryfall_card_printings", "tcgplayer_id")
