"""Renomme les tables Scryfall avec prefixe scryfall_

Revision ID: 20260609_rename_scryfall_tables
Revises: 20260609_drop_unused_tables
Create Date: 2026-06-09
"""
import os

from alembic import op

revision = "20260609_rename_scryfall_tables"
down_revision = "20260609_drop_unused_tables"
branch_labels = None
depends_on = None

_ECHAPPATOIRE = "MTGDB_ALLOW_DESTRUCTIVE_DOWNGRADE"


def _refuser_sauf_demande_explicite(degat: str) -> None:
    """
    Bloque un downgrade qui casserait les projets consommateurs.

    La base est partagée : ManaMind_AI, RELIC-Trade et mtgtrade lisent ces tables
    sous leurs noms actuels. Un `alembic downgrade -1` lancé par réflexe après une
    migration ratée les met hors service instantanément, sans que rien n'ait
    prévenu — la commande est documentée comme une opération ordinaire.

    Ce garde-fou suit la même convention que MTGDB_ALLOW_LOCAL_DB : il refuse par
    défaut et se lève explicitement, en connaissance de cause.
    """
    if os.getenv(_ECHAPPATOIRE, "").strip().lower() in ("1", "true", "yes", "on"):
        return
    raise RuntimeError(
        f"Downgrade REFUSÉ : {degat}\n"
        f"La base est partagée avec ManaMind_AI, RELIC-Trade et mtgtrade, qui "
        f"lisent ces objets. Ce downgrade les casse immédiatement.\n"
        f"Préférez corriger par une migration `upgrade`.\n"
        f"Si vous savez ce que vous faites : {_ECHAPPATOIRE}=1 alembic downgrade …"
    )


def upgrade() -> None:
    op.rename_table("mtg_sets", "scryfall_mtg_sets")
    op.rename_table("cards", "scryfall_cards")
    op.rename_table("card_faces", "scryfall_card_faces")
    op.rename_table("card_printings", "scryfall_card_printings")
    op.rename_table("card_prices", "scryfall_card_prices")


def downgrade() -> None:
    _refuser_sauf_demande_explicite(
        "les tables scryfall_* reprendraient leurs anciens noms (cards, "
        "card_printings, card_prices, card_faces, mtg_sets). Toute requête des "
        "consommateurs échouerait sur « relation does not exist »."
    )
    op.rename_table("scryfall_card_prices", "card_prices")
    op.rename_table("scryfall_card_printings", "card_printings")
    op.rename_table("scryfall_card_faces", "card_faces")
    op.rename_table("scryfall_cards", "cards")
    op.rename_table("scryfall_mtg_sets", "mtg_sets")
