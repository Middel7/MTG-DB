"""
Les parseurs Scryfall : ce qui traduit le bulk en lignes de base.

Ils n'avaient aucun test — non par négligence, mais parce qu'ils vivaient dans
`scripts/import_scryfall.py`, hors du wheel : les atteindre demandait de charger
1 000 lignes par `importlib.spec_from_file_location`. Depuis leur passage dans
`mtgdb.scryfall.parsers`, un import suffit.

L'enjeu est concret. Scryfall a déjà cassé l'import une fois en supprimant le
champ `download_uri` (28/07 → 25/08/2026, un mois sans que rien ne le signale).
Un champ qui change de forme sans disparaître serait pire : la colonne passerait
à `NULL` sans erreur, sur 542 827 impressions à la fois.

Tests purs : aucune base, aucun réseau.
"""
from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal

import pytest

from mtgdb.scryfall.parsers import (
    extract_printed_name,
    iter_bulk_cards,
    parse_card_row,
    parse_face_rows,
    parse_part_rows,
    parse_price_rows,
    parse_printing_row,
)

# ── Lecture du bulk ───────────────────────────────────────────────────────────

def _bulk(tmp_path, lignes: list[dict], nom="bulk.jsonl.gz"):
    chemin = tmp_path / nom
    with gzip.open(chemin, "wt", encoding="utf-8") as f:
        for ligne in lignes:
            f.write(json.dumps(ligne) + "\n")
    return chemin


def test_le_bulk_gzippe_se_lit_ligne_a_ligne(tmp_path):
    fichier = _bulk(tmp_path, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    assert [o["id"] for o in iter_bulk_cards(fichier)] == ["a", "b", "c"]


def test_les_crochets_d_un_tableau_json_sont_tolerés(tmp_path):
    """
    Scryfall est passé du tableau JSON au JSONL. Un retour en arrière ne doit pas
    faire échouer l'import sur un crochet isolé.
    """
    chemin = tmp_path / "tableau.jsonl.gz"
    with gzip.open(chemin, "wt", encoding="utf-8") as f:
        f.write('[\n{"id": "a"},\n{"id": "b"}\n]\n')
    assert [o["id"] for o in iter_bulk_cards(chemin)] == ["a", "b"]


# ── Carte (niveau oracle) ─────────────────────────────────────────────────────

def test_une_ligne_sans_oracle_id_est_ecartee():
    """
    Jetons et cartes d'art n'ont pas d'`oracle_id` : 82 lignes sur 542 909 au
    19/09. Elles n'ont pas de carte logique à laquelle se rattacher.
    """
    assert parse_card_row({"id": "x", "name": "Treasure"}) is None


@pytest.mark.parametrize("nom,attendu", [
    ("Juzám Djinn", "juzam djinn"),
    ("Lim-Dûl's Vault", "lim-dul's vault"),
    ("Sénéchal", "senechal"),
    ("Fire // Ice", "fire // ice"),          # le séparateur des cartes split survit
    ("  Sol Ring  ", "sol ring"),
    ("Æther Vial", "æther vial"),            # ligature : NFD ne la décompose pas
])
def test_le_nom_normalise_perd_ses_accents_et_sa_casse(nom, attendu):
    """
    `normalized_name` sert la recherche insensible aux accents. ManaMind_AI
    l'interroge : 2 327 995 parcours de son index relevés en base.
    """
    assert parse_card_row({"oracle_id": "o", "name": nom})["normalized_name"] == attendu


def test_le_cout_de_mana_est_repris_de_la_premiere_face_si_absent():
    """
    Les cartes double-face n'ont pas de `mana_cost` au niveau racine : il est sur
    chaque face. Sans ce repli, toutes les DFC auraient un coût nul.
    """
    ligne = parse_card_row({
        "oracle_id": "o", "name": "Delver of Secrets",
        "card_faces": [{"mana_cost": "{U}"}, {"mana_cost": ""}],
    })
    assert ligne["mana_cost"] == "{U}"


def test_la_legalite_commander_est_un_booleen_strict():
    for valeur, attendu in [("legal", True), ("not_legal", False),
                            ("banned", False), ("restricted", False)]:
        ligne = parse_card_row({"oracle_id": "o", "name": "x",
                                "legalities": {"commander": valeur}})
        assert ligne["legal_commander"] is attendu, valeur


def test_les_legalites_absentes_ne_font_pas_echouer():
    assert parse_card_row({"oracle_id": "o", "name": "x"})["legal_commander"] is False


@pytest.mark.parametrize("champ", ["colors", "color_identity", "keywords"])
def test_les_tableaux_absents_deviennent_des_listes_vides(champ):
    """`NULL` et « aucune couleur » ne se distinguent pas utilement ici."""
    assert parse_card_row({"oracle_id": "o", "name": "x"})[champ] == []


# ── Impression ────────────────────────────────────────────────────────────────

def test_une_date_de_sortie_malformee_ne_fait_pas_echouer_la_ligne():
    """Une impression vaut mieux sans date que pas d'impression du tout."""
    ligne = parse_printing_row({"id": "s", "released_at": "pas-une-date"}, card_id=1)
    assert ligne["released_at"] is None


def test_une_date_de_sortie_valide_est_convertie():
    ligne = parse_printing_row({"id": "s", "released_at": "2026-09-19"}, card_id=1)
    assert ligne["released_at"] == date(2026, 9, 19)


def test_les_images_sont_reprises_de_la_premiere_face_si_absentes():
    """Même raison que le coût de mana : les DFC n'ont pas d'`image_uris` racine."""
    ligne = parse_printing_row({
        "id": "s", "card_faces": [{"image_uris": {"normal": "https://exemple/1.jpg"}}],
    }, card_id=1)
    assert ligne["image_normal"] == "https://exemple/1.jpg"


def test_les_booleens_absents_valent_faux_et_non_null():
    """Les colonnes correspondantes sont `NOT NULL` : un `None` ferait échouer l'INSERT."""
    ligne = parse_printing_row({"id": "s"}, card_id=1)
    for champ in ("full_art", "promo", "reprint", "digital"):
        assert ligne[champ] is False, champ


def test_le_nom_traduit_des_cartes_multifaces_est_recompose():
    """
    C'est la colonne sur laquelle repose toute la recherche multilingue de
    RELIC-Trade. Une DFC porte un `printed_name` par face, pas au niveau racine.
    """
    assert extract_printed_name({
        "card_faces": [{"printed_name": "Feuervogel"}, {"printed_name": "Flammenmeer"}],
    }) == "Feuervogel // Flammenmeer"


def test_le_nom_traduit_racine_prime_sur_les_faces():
    assert extract_printed_name({"printed_name": "Wald", "card_faces": [
        {"printed_name": "ignoré"}]}) == "Wald"


def test_une_carte_anglaise_n_a_pas_de_nom_traduit():
    assert extract_printed_name({"name": "Forest"}) is None


# ── Prix ──────────────────────────────────────────────────────────────────────

def test_les_cinq_types_de_prix_sont_extraits():
    lignes = parse_price_rows(
        {"eur": "1.50", "eur_foil": "3.00", "usd": "1.75",
         "usd_foil": "4.00", "tix": "0.50"},
        printing_id=1, today=date(2026, 9, 19))
    assert {(ligne["currency"], ligne["price_type"]) for ligne in lignes} == {
        ("eur", "regular"), ("eur", "foil"), ("usd", "regular"),
        ("usd", "foil"), ("tix", "regular")}


def test_un_prix_absent_ne_produit_aucune_ligne():
    """
    Et non une ligne à zéro : la colonne est `Numeric`, et « pas de cote » n'est
    pas « vaut zéro euro ».
    """
    lignes = parse_price_rows({"eur": None, "usd": "2.00"},
                              printing_id=1, today=date(2026, 9, 19))
    assert len(lignes) == 1
    assert lignes[0]["currency"] == "usd"


def test_les_prix_sont_des_decimal_et_non_des_float():
    """La colonne est `Numeric(10, 2)` : il s'agit de monnaie."""
    ligne = parse_price_rows({"eur": "0.35"}, printing_id=1, today=date(2026, 9, 19))[0]
    assert isinstance(ligne["price"], Decimal)
    assert ligne["price"] == Decimal("0.35")


def test_un_prix_illisible_est_ignore_sans_emporter_les_autres():
    lignes = parse_price_rows({"eur": "n/a", "usd": "2.00"},
                              printing_id=1, today=date(2026, 9, 19))
    assert [ligne["currency"] for ligne in lignes] == ["usd"]


# ── Faces ─────────────────────────────────────────────────────────────────────

def test_une_carte_sans_faces_n_en_produit_aucune():
    assert parse_face_rows({"name": "Forest"}, card_id=1) == []


def test_chaque_face_porte_son_propre_texte_et_ses_images():
    faces = parse_face_rows({"card_faces": [
        {"name": "Fire", "mana_cost": "{R}", "oracle_text": "Deal 2 damage.",
         "image_uris": {"small": "https://exemple/f.jpg"}},
        {"name": "Ice", "mana_cost": "{U}", "oracle_text": "Tap target permanent."},
    ]}, card_id=42)

    assert [f["face_name"] for f in faces] == ["Fire", "Ice"]
    assert all(f["card_id"] == 42 for f in faces)
    assert faces[0]["image_small"] == "https://exemple/f.jpg"
    assert faces[1]["image_small"] is None


# ── Cartes liées ──────────────────────────────────────────────────────────────

def test_une_carte_sans_all_parts_ne_produit_aucune_liaison():
    assert parse_part_rows({"id": "x", "name": "Forest"}, card_id=1) == []


def test_les_jetons_sont_traduits_avec_leur_nom_et_leur_type():
    rows = parse_part_rows({
        "id": "source-0000",
        "all_parts": [
            {"id": "jeton-1111", "component": "token", "name": "Treasure",
             "type_line": "Token Artifact — Treasure"},
        ],
    }, card_id=7)
    assert rows == [{
        "card_id": 7,
        "component": "token",
        "part_scryfall_id": "jeton-1111",
        "part_name": "Treasure",
        "part_type_line": "Token Artifact — Treasure",
    }]


def test_la_carte_ne_figure_pas_parmi_ses_propres_parties():
    """
    Scryfall fait figurer la carte elle-même dans `all_parts`, en `combo_piece`.
    La garder reviendrait à compter la carte parmi les jetons qu'elle engendre.
    """
    rows = parse_part_rows({
        "id": "source-0000",
        "all_parts": [
            {"id": "source-0000", "component": "combo_piece", "name": "Academy Manufactor"},
            {"id": "jeton-2222", "component": "token", "name": "Clue"},
        ],
    }, card_id=3)
    assert [r["part_scryfall_id"] for r in rows] == ["jeton-2222"]


def test_un_meme_jeton_cite_deux_fois_n_est_retenu_qu_une_fois():
    """La table porte une contrainte d'unicité : un doublon ferait échouer le lot."""
    jeton = {"id": "jeton-3333", "component": "token", "name": "Soldier"}
    rows = parse_part_rows({"id": "s", "all_parts": [jeton, dict(jeton)]}, card_id=4)
    assert len(rows) == 1


def test_les_parties_de_fusion_sont_conservees():
    """
    `meld_part` et `meld_result` viennent du même champ : les écarter obligerait
    à relire le bulk le jour où l'on en aura besoin.
    """
    rows = parse_part_rows({
        "id": "bruna",
        "all_parts": [
            {"id": "gisela", "component": "meld_part", "name": "Gisela"},
            {"id": "brisela", "component": "meld_result", "name": "Brisela"},
        ],
    }, card_id=5)
    assert {r["component"] for r in rows} == {"meld_part", "meld_result"}
