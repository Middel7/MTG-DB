"""
Non-régression sur la perte d'impressions à l'import Scryfall.

Le 19/09/2026, un audit a mesuré que `_flush_batch()` écartait **22 037 des
542 827 lignes** du bulk à chaque run. La cause tenait en une ligne : la
déduplication par `oracle_id`, indispensable pour l'upsert des CARTES
(`ON CONFLICT DO UPDATE` ne peut pas affecter deux fois la même ligne), était
appliquée aussi à `raw_cards`, qui porte les IMPRESSIONS.

Toutes les impressions partageant un `oracle_id` à l'intérieur d'un même lot de
500 étaient donc jetées, sauf une. Le bulk étant trié par `scryfall_id` (UUID),
les collisions étaient fréquentes — surtout sur les terrains de base, qui
comptent des centaines d'impressions.

Rien ne le signalait : le compteur affiché était celui des survivantes. Le seul
symptôme visible était `cards_imported == printings_imported` dans chaque
journal, une égalité pourtant impossible pour 38 907 cartes et 539 629
impressions.

Ces tests n'ont besoin d'aucune base : ils observent ce que `_flush_batch`
transmet aux fonctions d'upsert, remplacées par des espions.
"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def import_scryfall():
    """Charge le script par son chemin : `scripts/` n'est pas un paquet importable."""
    spec = importlib.util.spec_from_file_location(
        "import_scryfall_flush", ROOT / "scripts" / "import_scryfall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _impression(scryfall_id: str, oracle_id: str, *, lang: str = "en",
                faces: list[dict] | None = None) -> dict:
    """Ligne de bulk minimale, réduite aux champs que lisent les parseurs."""
    brut = {
        "id": scryfall_id,
        "oracle_id": oracle_id,
        "name": "Forest",
        "lang": lang,
        "set": "blb",
        "collector_number": "280",
        "prices": {},
    }
    if faces:
        brut["card_faces"] = faces
    return brut


@pytest.fixture
def espions(import_scryfall, monkeypatch):
    """
    Remplace les écritures par des espions et retourne ce qui leur a été soumis.

    On n'observe pas la base : ce qui est en cause est ce que `_flush_batch`
    décide d'envoyer, pas la façon dont PostgreSQL l'enregistre.
    """
    recu: dict[str, list] = {"cartes": [], "impressions": [], "faces": [], "card_ids": []}

    def faux_upsert_cards(session, rows):
        recu["cartes"] = list(rows)
        return {r["oracle_id"]: 1000 + i for i, r in enumerate(rows)}

    def faux_upsert_printings(session, rows):
        recu["impressions"] = list(rows)
        return {r["scryfall_id"]: 2000 + i for i, r in enumerate(rows)}

    def fausses_faces(session, face_rows, card_ids):
        recu["faces"] = list(face_rows)
        recu["card_ids"] = list(card_ids)

    monkeypatch.setattr(import_scryfall, "_upsert_cards", faux_upsert_cards)
    monkeypatch.setattr(import_scryfall, "_upsert_printings", faux_upsert_printings)
    monkeypatch.setattr(import_scryfall, "_replace_faces", fausses_faces)
    monkeypatch.setattr(import_scryfall, "_insert_prices", lambda session, rows: None)
    return recu


class _SessionMuette:
    """Suffit : toutes les écritures réelles sont interceptées par les espions."""

    def commit(self) -> None:
        pass


def test_trois_impressions_d_une_meme_carte_sont_toutes_conservees(import_scryfall, espions):
    """
    Le coeur du correctif.

    Trois langues de la même carte dans un même lot : trois impressions doivent
    partir à l'upsert. Avant le correctif, il n'en restait qu'une.
    """
    oracle = "aaaaaaaa-0000-0000-0000-000000000001"
    bruts = [
        _impression("11111111-0000-0000-0000-000000000001", oracle, lang="en"),
        _impression("22222222-0000-0000-0000-000000000002", oracle, lang="fr"),
        _impression("33333333-0000-0000-0000-000000000003", oracle, lang="ja"),
    ]
    lignes_cartes = [import_scryfall._parse_card_row(b) for b in bruts]

    cartes, impressions = import_scryfall._flush_batch(
        _SessionMuette(), lignes_cartes, bruts, date(2026, 9, 19))

    assert impressions == 3, "les trois langues doivent être upsertées"
    assert cartes == 1, "elles ne désignent qu'une seule carte oracle"
    assert {r["scryfall_id"] for r in espions["impressions"]} == {b["id"] for b in bruts}


def test_la_carte_oracle_n_est_upsertee_qu_une_fois(import_scryfall, espions):
    """`ON CONFLICT DO UPDATE` ne peut pas affecter deux fois la même ligne."""
    oracle = "aaaaaaaa-0000-0000-0000-000000000002"
    bruts = [
        _impression("11111111-0000-0000-0000-00000000000a", oracle, lang="en"),
        _impression("22222222-0000-0000-0000-00000000000b", oracle, lang="fr"),
        _impression("33333333-0000-0000-0000-00000000000c", oracle, lang="de"),
        _impression("44444444-0000-0000-0000-00000000000d", oracle, lang="es"),
    ]
    lignes_cartes = [import_scryfall._parse_card_row(b) for b in bruts]

    import_scryfall._flush_batch(_SessionMuette(), lignes_cartes, bruts, date(2026, 9, 19))

    oracle_ids = [r["oracle_id"] for r in espions["cartes"]]
    assert oracle_ids == [oracle], "un seul enregistrement par oracle_id, sinon PostgreSQL refuse"


def test_les_faces_ne_sont_pas_dupliquees_par_les_impressions_multiples(import_scryfall, espions):
    """
    Effet de bord du correctif, à ne pas laisser passer.

    Les faces appartiennent à la CARTE. Depuis que plusieurs impressions d'une
    même carte cohabitent dans un lot, les parser à chaque fois insérerait les
    mêmes faces autant de fois — et `scryfall_card_faces` n'a aucune contrainte
    d'unicité pour l'en empêcher.
    """
    oracle = "aaaaaaaa-0000-0000-0000-000000000003"
    faces = [{"name": "Fire", "mana_cost": "{R}"}, {"name": "Ice", "mana_cost": "{U}"}]
    bruts = [
        _impression("44444444-0000-0000-0000-000000000004", oracle, lang="en", faces=faces),
        _impression("55555555-0000-0000-0000-000000000005", oracle, lang="de", faces=faces),
    ]
    lignes_cartes = [import_scryfall._parse_card_row(b) for b in bruts]

    import_scryfall._flush_batch(_SessionMuette(), lignes_cartes, bruts, date(2026, 9, 19))

    assert len(espions["faces"]) == 2, "deux faces au total, pas quatre"
    assert len(espions["card_ids"]) == len(set(espions["card_ids"])), (
        "un card_id ne doit apparaître qu'une fois dans le DELETE préalable"
    )


def test_une_impression_en_double_dans_le_lot_est_bien_dedupliquee(import_scryfall, espions):
    """La déduplication légitime — celle sur l'identité de l'impression — subsiste."""
    oracle = "aaaaaaaa-0000-0000-0000-000000000004"
    meme_id = "66666666-0000-0000-0000-000000000006"
    bruts = [_impression(meme_id, oracle), _impression(meme_id, oracle)]
    lignes_cartes = [import_scryfall._parse_card_row(b) for b in bruts]

    _, impressions = import_scryfall._flush_batch(
        _SessionMuette(), lignes_cartes, bruts, date(2026, 9, 19))

    assert impressions == 1, "deux fois le même scryfall_id reste une seule impression"


def test_les_compteurs_ne_sont_plus_egaux_par_construction(import_scryfall, espions):
    """
    Garde-fou contre la récidive.

    `cards_imported == printings_imported` était LA signature du défaut. Avec des
    impressions multiples par carte, les deux compteurs doivent diverger.
    """
    bruts = [
        _impression("77777777-0000-0000-0000-000000000007", "bbbbbbbb-0000-0000-0000-000000000001"),
        _impression("88888888-0000-0000-0000-000000000008", "bbbbbbbb-0000-0000-0000-000000000001"),
        _impression("99999999-0000-0000-0000-000000000009", "bbbbbbbb-0000-0000-0000-000000000002"),
    ]
    lignes_cartes = [import_scryfall._parse_card_row(b) for b in bruts]

    cartes, impressions = import_scryfall._flush_batch(
        _SessionMuette(), lignes_cartes, bruts, date(2026, 9, 19))

    assert (cartes, impressions) == (2, 3)
    assert cartes != impressions
