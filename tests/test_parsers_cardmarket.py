"""
Les parseurs Cardmarket, la couche la plus exposée du pipeline.

Ils traduisent un JSON dont NOUS ne maîtrisons rien : Cardmarket peut renommer
une clé, changer une casse, passer d'un point à une virgule décimale. Le code
l'anticipe en acceptant plusieurs orthographes par champ
(`_PRICE_KEY_MAP` en compte 4 à 6 par prix), mais aucune de ces variantes
n'était vérifiée.

L'enjeu est précis : un renommage non couvert ne provoque aucune erreur. Le
champ devient simplement `None`, la ligne s'insère, et un prix disparaît en
silence de toute la base — pour les 122 299 produits à la fois.

Ces tests sont purs : aucune base, aucun réseau.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mtgdb.cardmarket.parsers import (
    extract_price_guide_list,
    extract_products_list,
    parse_price_guide_entry,
    parse_product,
)

# ── Product Catalog ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("cle_id", ["idProduct", "id_product", "IdProduct"])
def test_les_trois_orthographes_de_l_identifiant_produit(cle_id):
    resultat = parse_product({cle_id: 12345, "enName": "Sol Ring"})
    assert resultat is not None
    assert resultat["id_product"] == 12345


def test_un_identifiant_en_chaine_est_converti():
    """Cardmarket a déjà livré des identifiants en texte."""
    assert parse_product({"idProduct": "998877"})["id_product"] == 998877


@pytest.mark.parametrize("valeur", [None, "", 0, "abc", {}])
def test_un_produit_sans_identifiant_exploitable_est_rejete(valeur):
    """
    Rejeté, et non inséré avec un identifiant nul.

    Une ligne sans `id_product` ne se rattache à rien : la garder ne ferait
    qu'alimenter la base de lignes que personne ne peut joindre.
    """
    assert parse_product({"idProduct": valeur}) is None


def test_id_expansion_est_bien_extrait():
    """
    Seul champ du Product Catalog qui distingue deux produits de même nom.

    Son absence avait rendu indiscernables deux « Jace Reawakened » dont les prix
    différaient d'un facteur 20 (migration 20260826).
    """
    assert parse_product({"idProduct": 1, "idExpansion": 5662})["id_expansion"] == 5662


def test_le_json_brut_est_conserve_tel_quel():
    """`raw_json` est le seul filet quand une clé change de nom sans prévenir."""
    brut = {"idProduct": 42, "champInconnuDuFutur": "valeur"}
    assert parse_product(brut)["raw_json"] == brut


@pytest.mark.parametrize("enveloppe,attendu", [
    ([{"idProduct": 1}], 1),
    ({"product": [{"idProduct": 1}, {"idProduct": 2}]}, 2),
    ({"products": [{"idProduct": 1}]}, 1),
    ({"data": [{"idProduct": 1}]}, 1),
    ({"singles": [{"idProduct": 1}]}, 1),
    ({"cle_inconnue": [{"idProduct": 1}]}, 0),
    ({}, 0),
])
def test_les_formes_d_enveloppe_acceptees(enveloppe, attendu):
    assert len(extract_products_list(enveloppe)) == attendu


# ── Price Guide ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("champ,cles", [
    ("avg_price", ["avg", "Avg", "avgPrice", "avg_price", "AVG"]),
    ("low_price", ["low", "Low", "lowPrice", "low_price", "LOW"]),
    ("trend_price", ["trend", "Trend", "trendPrice", "trend_price", "TREND"]),
    ("foil_low", ["low-foil", "foilLow", "foil_low", "FoilLow", "Foil Low"]),
    ("foil_trend", ["trend-foil", "foilTrend", "foil_trend", "FoilTrend", "Foil Trend"]),
    ("avg30", ["avg30", "Avg30", "AVG30"]),
    ("foil_avg30", ["avg30-foil", "foilAvg30", "foil_avg30", "FoilAvg30"]),
    ("suggested_price", ["suggestedPrice", "suggested_price", "SuggestedPrice", "sell", "Sell"]),
    ("low_price_ex_plus", ["lowEx", "lowPriceExPlus", "low_price_ex_plus",
                           "Low Price Ex+", "lowExPlus"]),
])
def test_toutes_les_orthographes_de_prix_sont_reconnues(champ, cles):
    """
    Le test qui protège contre la disparition silencieuse d'un prix.

    Si une variante cesse d'être reconnue, la colonne passe à NULL sans la
    moindre erreur, et personne ne le voit avant qu'un client ne signale un prix
    manquant.
    """
    for cle in cles:
        entree = parse_price_guide_entry({"idProduct": 1, cle: "12.34"})
        assert entree[champ] == Decimal("12.34"), f"clé « {cle} » non reconnue"


def test_la_virgule_decimale_est_acceptee():
    """Cardmarket est allemand : la virgule décimale n'est pas une hypothèse d'école."""
    assert parse_price_guide_entry({"idProduct": 1, "avg": "1,50"})["avg_price"] == Decimal("1.50")


def test_zero_signifie_absence_de_donnee_et_non_gratuit():
    """
    Cardmarket renvoie 0 quand il n'a pas de relevé.

    L'enregistrer tel quel ferait passer une carte sans cote pour une carte sans
    valeur — et RELIC-Trade prend des décisions de rachat sur ces chiffres.
    """
    assert parse_price_guide_entry({"idProduct": 1, "avg": 0})["avg_price"] is None
    assert parse_price_guide_entry({"idProduct": 1, "avg": "0.00"})["avg_price"] is None


def test_un_prix_illisible_devient_none_sans_faire_echouer_la_ligne():
    """Une valeur aberrante ne doit pas emporter les 15 autres prix de l'entrée."""
    entree = parse_price_guide_entry({"idProduct": 1, "avg": "n/a", "trend": "3.50"})
    assert entree["avg_price"] is None
    assert entree["trend_price"] == Decimal("3.50")


def test_la_precision_monetaire_est_preservee():
    """
    `Decimal`, jamais `float`.

    La colonne est `Numeric(12, 4)` : une conversion par flottant introduirait une
    imprécision là où elle est le moins acceptable.
    """
    valeur = parse_price_guide_entry({"idProduct": 1, "avg": "0.1"})["avg_price"]
    assert isinstance(valeur, Decimal)
    assert valeur == Decimal("0.1")


@pytest.mark.parametrize("enveloppe,attendu", [
    ([{"idProduct": 1}], 1),
    ({"priceGuides": [{"idProduct": 1}]}, 1),
    ({"priceGuide": [{"idProduct": 1}, {"idProduct": 2}]}, 2),
    ({"data": [{"idProduct": 1}]}, 1),
    ({"autre": [{"idProduct": 1}]}, 0),
])
def test_les_enveloppes_du_price_guide(enveloppe, attendu):
    assert len(extract_price_guide_list(enveloppe)) == attendu
