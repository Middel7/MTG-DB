"""
Sélection des étapes et contrat de sortie de l'orchestrateur.

`--only` et `--skip` décident de ce qui tourne sur une base de production deux
fois par jour, et `render.yaml` les utilise dans ses deux Cron Jobs. Aucun n'était
vérifié : seul le code de sortie 2 (verrou tenu) avait un test.

Ces tests sont purs — `select_steps` ne touche ni au réseau ni à la base.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def update_all():
    spec = importlib.util.spec_from_file_location(
        "update_all_sous_test", ROOT / "scripts" / "update_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli(**options) -> argparse.Namespace:
    base = {"only": None, "skip": None}
    base.update(options)
    return argparse.Namespace(**base)


def _cles(etapes) -> list[str]:
    return [etape[0] for etape in etapes]


def test_sans_option_les_quatre_etapes_tournent(update_all):
    assert _cles(update_all.select_steps(_cli())) == [
        "scryfall", "cardmarket", "game-changers", "tags"]


def test_skip_tags_est_la_commande_du_cron_quotidien(update_all):
    """`dockerCommand: python scripts/update_all.py --skip tags` dans render.yaml."""
    assert _cles(update_all.select_steps(_cli(skip=["tags"]))) == [
        "scryfall", "cardmarket", "game-changers"]


def test_only_tags_est_la_commande_du_cron_hebdomadaire(update_all):
    """`dockerCommand: python scripts/update_all.py --only tags` dans render.yaml."""
    assert _cles(update_all.select_steps(_cli(only=["tags"]))) == ["tags"]


def test_only_preserve_l_ordre_canonique(update_all):
    """
    L'ordre ne suit pas la ligne de commande mais les dépendances.

    Cardmarket lie ses produits aux impressions que Scryfall vient d'écrire :
    l'inverser produirait un rapport de liaison faux.
    """
    assert _cles(update_all.select_steps(_cli(only=["cardmarket", "scryfall"]))) == [
        "scryfall", "cardmarket"]


def test_skip_multiple(update_all):
    etapes = update_all.select_steps(_cli(skip=["tags", "game-changers"]))
    assert _cles(etapes) == ["scryfall", "cardmarket"]


@pytest.mark.parametrize("option", ["only", "skip"])
def test_une_etape_inconnue_arrete_le_run(update_all, option):
    """
    Plutôt que d'être ignorée en silence.

    Une faute de frappe dans un `dockerCommand` Render — `--skip tag` au lieu de
    `--skip tags` — doit se voir tout de suite, et non faire tourner pendant
    quarante minutes une étape qu'on croyait exclue.
    """
    with pytest.raises(SystemExit):
        update_all.select_steps(_cli(**{option: ["scryfal"]}))


def test_only_l_emporte_sur_skip(update_all):
    """
    Comportement existant, figé ici parce qu'il n'est écrit nulle part.

    Les deux options ensemble sont une commande ambiguë ; `--only` gagne. Le test
    documente le choix plutôt que de le laisser se découvrir en production.
    """
    assert _cles(update_all.select_steps(_cli(only=["tags"], skip=["tags"]))) == ["tags"]


def test_l_ordre_canonique_place_scryfall_en_premier(update_all):
    """
    Cardmarket, Game Changers et les tags lisent tous ce que Scryfall a écrit.

    Un réordonnancement accidentel de la liste `STEPS` ferait travailler les trois
    autres étapes sur le catalogue de la veille.
    """
    assert update_all.STEPS[0][0] == "scryfall"
    assert _cles(update_all.STEPS).index("cardmarket") > _cles(update_all.STEPS).index("scryfall")


# ── Délai maximal par étape ───────────────────────────────────────────────────

def test_chaque_etape_porte_un_delai_maximal(update_all):
    """
    Sans délai, une étape bloquée bloque l'orchestrateur indéfiniment.

    Les deux filets qui existaient — 3 h côté Planificateur Windows, 12 h côté
    Render — sont EXTÉRIEURS au programme : ils ne s'appliquent pas à un run
    lancé à la main, et tuent la tâche sans laisser de trace exploitable.
    """
    for cle, _libelle, _script, _args, delai in update_all.STEPS:
        assert isinstance(delai, (int, float)), cle
        assert delai > 0, cle


def test_les_delais_laissent_de_la_marge_sur_les_durees_mesurees(update_all):
    """
    Un délai trop serré est pire que pas de délai : il tue des runs sains.

    Repères mesurés en production : 84 min pour Scryfall, ~10 min pour
    Cardmarket, ~40 min pour les tags. Chaque plafond doit rester nettement
    au-dessus.
    """
    plafonds = {cle: delai for cle, _l, _s, _a, delai in update_all.STEPS}
    assert plafonds["scryfall"] >= 3 * 3600, "84 min mesurées en production"
    assert plafonds["cardmarket"] >= 3600
    assert plafonds["tags"] >= 2 * 3600, "~40 min, et 0,2 s de pause par carte"
    assert plafonds["game-changers"] >= 300


def test_un_depassement_est_rapporte_comme_un_echec(update_all, tmp_path):
    """
    Et non comme un succès.

    Un processus qu'on vient de tuer peut rendre n'importe quel code ; c'est
    l'orchestrateur qui décide, pas lui.
    """
    import sys

    sortie = update_all.Output(None)
    code = update_all._stream_subprocess(
        # Un processus qui ne rend jamais la main.
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=dict(__import__("os").environ),
        out=sortie,
        timeout=1.0,
    )
    assert code == 1, "un dépassement doit valoir échec"
