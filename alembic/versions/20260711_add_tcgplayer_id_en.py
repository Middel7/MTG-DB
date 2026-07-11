"""Ajoute tcgplayer_id_en à scryfall_card_printings

Revision ID: 20260711_add_tcgplayer_id_en
Revises: 20260710_add_tcgplayer_id
Create Date: 2026-07-11
"""
import sqlalchemy as sa
from alembic import op

revision = "20260711_add_tcgplayer_id_en"
down_revision = "20260710_add_tcgplayer_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scryfall_card_printings",
        sa.Column("tcgplayer_id_en", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_scryfall_card_printings_tcgplayer_id_en",
        "scryfall_card_printings",
        ["tcgplayer_id_en"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scryfall_card_printings_tcgplayer_id_en",
        table_name="scryfall_card_printings",
    )
    op.drop_column("scryfall_card_printings", "tcgplayer_id_en")
