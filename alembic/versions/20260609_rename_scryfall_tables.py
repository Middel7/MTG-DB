"""Renomme les tables Scryfall avec prefixe scryfall_

Revision ID: 20260609_rename_scryfall_tables
Revises: 20260609_drop_unused_tables
Create Date: 2026-06-09
"""
from alembic import op

revision = "20260609_rename_scryfall_tables"
down_revision = "20260609_drop_unused_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("mtg_sets", "scryfall_mtg_sets")
    op.rename_table("cards", "scryfall_cards")
    op.rename_table("card_faces", "scryfall_card_faces")
    op.rename_table("card_printings", "scryfall_card_printings")
    op.rename_table("card_prices", "scryfall_card_prices")


def downgrade() -> None:
    op.rename_table("scryfall_card_prices", "card_prices")
    op.rename_table("scryfall_card_printings", "card_printings")
    op.rename_table("scryfall_card_faces", "card_faces")
    op.rename_table("scryfall_cards", "cards")
    op.rename_table("scryfall_mtg_sets", "mtg_sets")
