"""Aligne les noms d'index sur les noms de tables préfixés scryfall_/cardmarket_

Quand les tables ont été renommées (cards -> scryfall_cards, etc.), leurs index ont
gardé leur ancien nom. L'autogenerate voyait donc en permanence 15 index « à
supprimer » et 15 « à créer », un bruit qui se serait mêlé à toute vraie migration.

On utilise ALTER INDEX ... RENAME TO plutôt que le drop + create que génère Alembic :
c'est une opération de métadonnée pure, instantanée, là où un drop + create
reconstruirait les index de tables de 500 000 lignes.

IF EXISTS rend la migration rejouable sur une base déjà alignée (ex. une base neuve
créée directement avec les bons noms).

Revision ID: 20260713_rename_legacy_indexes
Revises: 1b7a9f3835e5
Create Date: 2026-07-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260713_rename_legacy_indexes"
down_revision: Union[str, Sequence[str], None] = "1b7a9f3835e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (ancien nom, nouveau nom)
RENAMES: list[tuple[str, str]] = [
    ("ix_card_faces_card_id", "ix_scryfall_card_faces_card_id"),
    ("ix_card_prices_date", "ix_scryfall_card_prices_date"),
    ("ix_card_prices_printing_id", "ix_scryfall_card_prices_printing_id"),
    ("ix_card_printings_card_id", "ix_scryfall_card_printings_card_id"),
    ("ix_card_printings_cardmarket_id", "ix_scryfall_card_printings_cardmarket_id"),
    ("ix_card_printings_oracle_id", "ix_scryfall_card_printings_oracle_id"),
    ("ix_card_printings_printed_name", "ix_scryfall_card_printings_printed_name"),
    ("ix_card_printings_scryfall_id", "ix_scryfall_card_printings_scryfall_id"),
    ("ix_card_printings_set_code", "ix_scryfall_card_printings_set_code"),
    ("ix_cards_game_changer", "ix_scryfall_cards_game_changer"),
    ("ix_cards_name", "ix_scryfall_cards_name"),
    ("ix_cards_normalized_name", "ix_scryfall_cards_normalized_name"),
    ("ix_cards_oracle_id", "ix_scryfall_cards_oracle_id"),
    ("ix_cm_price_guide_import_file_id", "ix_cardmarket_price_guide_entries_import_file_id"),
    ("ix_mtg_sets_code", "ix_scryfall_mtg_sets_code"),
]


def upgrade() -> None:
    for old, new in RENAMES:
        op.execute(f'ALTER INDEX IF EXISTS "{old}" RENAME TO "{new}"')


def downgrade() -> None:
    for old, new in RENAMES:
        op.execute(f'ALTER INDEX IF EXISTS "{new}" RENAME TO "{old}"')
