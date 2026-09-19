"""
Non-régression sur le refus d'une base locale.

`update-prod.ps1` refusait de lancer un run « production » dont l'URL pointait
sur localhost — sans quoi le run met à jour la base de développement et rend un
rapport final tout vert. Ce garde-fou est passé en Python pour couvrir aussi le
Cron Job Render, où aucun script PowerShell n'intervient.
"""
from __future__ import annotations

import pytest

from mtgdb.db.engine import LocalDatabaseRefused, assert_remote_database

LOCALE = "postgresql://postgres:pass@localhost:5432/manamind"
DISTANTE = "postgresql://u:p@dpg-abc.frankfurt-postgres.render.com/relictrade"


def test_hors_conteneur_une_base_locale_est_acceptee():
    # Le cas nominal du poste de développement : rien ne doit gêner.
    assert_remote_database(LOCALE)


def test_en_conteneur_une_base_locale_est_refusee(monkeypatch):
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    with pytest.raises(LocalDatabaseRefused) as exc:
        assert_remote_database(LOCALE)
    assert "conteneur" in str(exc.value)


def test_en_conteneur_le_mot_de_passe_reste_masque(monkeypatch):
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    with pytest.raises(LocalDatabaseRefused) as exc:
        assert_remote_database(LOCALE)
    assert "pass@" not in str(exc.value)


def test_en_conteneur_une_base_distante_passe(monkeypatch):
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    assert_remote_database(DISTANTE)


def test_le_flag_de_rattrapage_arme_le_garde_fou_hors_conteneur(monkeypatch):
    # C'est ce que pose update-prod.ps1 : hors conteneur, rien ne distingue
    # autrement un run « prod » d'un run local.
    monkeypatch.setenv("MTGDB_REQUIRE_REMOTE_DB", "1")
    with pytest.raises(LocalDatabaseRefused):
        assert_remote_database(LOCALE)


def test_l_echappatoire_explicite_est_respectee(monkeypatch):
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    monkeypatch.setenv("MTGDB_ALLOW_LOCAL_DB", "1")
    assert_remote_database(LOCALE)


def test_le_prefixe_legacy_est_normalise_avant_controle(monkeypatch):
    # Une URL postgres:// ne doit pas échapper au contrôle par sa seule forme.
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    with pytest.raises(LocalDatabaseRefused):
        assert_remote_database("postgres://postgres:pass@127.0.0.1:5432/manamind")


def test_une_url_absente_est_signalee(monkeypatch):
    monkeypatch.setenv("MTGDB_CONTAINER", "1")
    with pytest.raises(RuntimeError, match="DATABASE_URL absent"):
        assert_remote_database("")
