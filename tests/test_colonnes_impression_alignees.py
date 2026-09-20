"""
Le parseur, la table et l'UPDATE doivent parler des mêmes colonnes.

Une impression suit trois chemins qui se déclarent séparément :

1. `parse_printing_row()` produit un dictionnaire depuis le bulk Scryfall ;
2. le modèle `CardPrinting` décrit la table ;
3. `COLONNES_IMPRESSION` liste ce que l'UPDATE réécrit sur une impression
   **déjà connue** (`upsert_printings`).

Rien ne les relie. Une colonne ajoutée au parseur mais oubliée dans
`COLONNES_IMPRESSION` se comporte de la pire façon possible : elle est bien
écrite à la CRÉATION de l'impression, donc elle marche sur une base neuve et
dans tous les tests — mais elle n'est **jamais mise à jour** ensuite. Sur une
base de production où les impressions existent déjà toutes, la colonne reste
vide indéfiniment, sans la moindre erreur.

Ces tests sont purs : aucune base, aucun réseau.
"""

from __future__ import annotations

from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.scryfall.parsers import parse_printing_row
from mtgdb.scryfall.upserts import COLONNES_IMPRESSION

#: Clés produites par le parseur qui ne sont pas réécrites par l'UPDATE, et
#: pourquoi. Toute autre absence est un oubli.
HORS_UPDATE = {
    # Clé métier de l'impression : c'est le critère du WHERE, pas une valeur à
    # réécrire.
    "scryfall_id",
}


def _cles_du_parseur() -> set[str]:
    return set(parse_printing_row({"id": "x"}, card_id=1))


def test_toute_colonne_produite_par_le_parseur_est_reecrite_par_l_update():
    manquantes = _cles_du_parseur() - set(COLONNES_IMPRESSION) - HORS_UPDATE
    assert not manquantes, (
        "Colonnes produites par parse_printing_row() mais absentes de "
        f"COLONNES_IMPRESSION : {sorted(manquantes)}. Elles seraient écrites à la "
        "création d'une impression et jamais mises à jour ensuite — invisible sur "
        "une base neuve, définitif sur la production."
    )


def test_l_update_ne_reecrit_aucune_colonne_que_le_parseur_ne_produit_pas():
    """L'inverse compte aussi : une colonne listée sans être produite serait
    écrasée par `NULL` à chaque run."""
    orphelines = set(COLONNES_IMPRESSION) - _cles_du_parseur()
    assert not orphelines, (
        f"Colonnes réécrites sans être produites par le parseur : {sorted(orphelines)}"
    )


def test_toute_colonne_produite_par_le_parseur_existe_dans_la_table():
    """Le garde-fou qui manquait à la purge des prix (docs/BUGS du 2026-09-19 de
    RELIC-Trade) : une colonne déclarée mais inexistante ne se voit qu'à
    l'exécution, contre une vraie base PostgreSQL."""
    colonnes_table = {c.key for c in CardPrinting.__table__.columns}
    inconnues = _cles_du_parseur() - colonnes_table
    assert not inconnues, (
        f"Colonnes produites par le parseur et absentes de la table : {sorted(inconnues)}"
    )


def test_la_qualite_du_visuel_suit_les_trois_chemins():
    """Cas nommé, parce que c'est celui qui a motivé ces garde-fous : sans
    `image_status`, rien ne distingue un scan d'un carton « Localized Image Not
    Available », et la vitrine de RELIC-Trade affichait le carton."""
    assert "image_status" in _cles_du_parseur()
    assert "image_status" in COLONNES_IMPRESSION
    assert "image_status" in {c.key for c in CardPrinting.__table__.columns}
