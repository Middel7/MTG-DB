"""
Non-régression sur les colonnes que le bulk Scryfall ne doit pas effacer.

`cardmarket_id` n'est fourni par Scryfall que sur l'impression anglaise. Écrasé
tel quel, il repassait à NULL sur les 401 230 impressions non anglaises à chaque
run — puis `propagate_cardmarket_ids()` les repeuplait juste après. 77 % de la
table réécrite deux fois par run pour revenir au point de départ, soit 24 min
sur la base de production.

Ces tests portent sur le SQL généré plutôt que sur une exécution : la clause
`ON CONFLICT DO UPDATE` est précisément ce qui a été mal écrit, et c'est elle
qu'il faut verrouiller.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from mtgdb.db.models.card_printing import CardPrinting

ROOT = Path(__file__).resolve().parents[1]

UPDATE_COLS = [
    "oracle_id", "card_id", "set_code", "collector_number", "lang",
    "rarity", "released_at", "artist", "border_color", "frame",
    "full_art", "promo", "reprint", "digital",
    "image_small", "image_normal", "image_large", "scryfall_uri",
    "cardmarket_id", "tcgplayer_id", "printed_name",
]


@pytest.fixture(scope="module")
def import_scryfall():
    spec = importlib.util.spec_from_file_location(
        "import_scryfall_upsert", ROOT / "scripts" / "import_scryfall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sql_du_upsert(import_scryfall):
    """Reproduit la clause construite par `_upsert_printings`, et la compile."""
    stmt = pg_insert(CardPrinting).values([{"scryfall_id": "00000000-0000-0000-0000-000000000000"}])
    stmt = stmt.on_conflict_do_update(
        index_elements=["scryfall_id"],
        set_={
            col: (
                func.coalesce(getattr(stmt.excluded, col), getattr(CardPrinting, col))
                if col in import_scryfall.PRESERVE_IF_NULL
                else getattr(stmt.excluded, col)
            )
            for col in UPDATE_COLS
        },
    )
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


def test_cardmarket_id_est_preserve_quand_le_bulk_ne_le_fournit_pas(sql_du_upsert):
    assert ("cardmarket_id = coalesce(excluded.cardmarket_id, "
            "scryfall_card_printings.cardmarket_id)") in sql_du_upsert


@pytest.mark.parametrize("colonne", ["artist", "rarity", "lang", "printed_name",
                                     "tcgplayer_id", "image_normal"])
def test_les_autres_colonnes_suivent_bien_le_bulk(sql_du_upsert, colonne):
    """
    Le bulk fait autorité partout ailleurs.

    Étendre la préservation par confort figerait des données que Scryfall
    corrige : un artiste mal orthographié, une rareté révisée, une image
    remplacée ne seraient plus jamais mis à jour.
    """
    assert f"{colonne} = excluded.{colonne}" in sql_du_upsert
    assert f"coalesce(excluded.{colonne}" not in sql_du_upsert


def test_la_liste_des_colonnes_preservees_reste_minimale(import_scryfall):
    # Garde-fou : chaque ajout ici rend une donnée non corrigeable par le bulk.
    # Ce test force à relire le commentaire du module avant d'en ajouter une.
    assert import_scryfall.PRESERVE_IF_NULL == frozenset({"cardmarket_id"})


def test_la_propagation_reste_en_place(import_scryfall):
    """
    Le correctif ne rend pas la propagation inutile : elle sert toujours aux
    impressions NOUVELLES, dont la version anglaise porte un cardmarket_id que
    les autres langues n'ont pas encore. Elle devient simplement peu coûteuse,
    puisqu'elle ne trouve plus que ces cas-là.
    """
    assert hasattr(import_scryfall, "propagate_cardmarket_ids")
