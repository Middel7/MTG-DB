"""
Non-régression sur le comportement en conteneur.

Sur un Cron Job Render, le disque est éphémère : un `logs/update_<date>.log`
part avec le conteneur. Render capture stdout, qui devient la seule trace
exploitable d'un run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mtgdb.runtime import env_flag, in_container

ROOT = Path(__file__).resolve().parents[1]


class TestDetection:
    def test_hors_conteneur_par_defaut(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        assert in_container() is False

    def test_le_signal_du_dockerfile_fait_autorite(self, monkeypatch):
        monkeypatch.setenv("MTGDB_CONTAINER", "1")
        assert in_container() is True

    def test_le_signal_du_dockerfile_peut_forcer_le_mode_poste(self, monkeypatch):
        # MTGDB_CONTAINER=0 au lancement doit l'emporter sur RENDER : c'est ce
        # qui permet de reproduire un incident avec la journalisation fichier.
        monkeypatch.setenv("MTGDB_CONTAINER", "0")
        monkeypatch.setenv("RENDER", "true")
        assert in_container() is False

    def test_render_est_reconnu(self, monkeypatch):
        monkeypatch.setenv("RENDER", "true")
        assert in_container() is True

    @pytest.mark.parametrize("valeur,attendu", [("1", True), ("true", True), ("ON", True),
                                                ("0", False), ("", False), ("non", False)])
    def test_lecture_des_booleens(self, monkeypatch, valeur, attendu):
        monkeypatch.setenv("MTGDB_TEST_FLAG", valeur)
        assert env_flag("MTGDB_TEST_FLAG") is attendu


@pytest.mark.integration
def test_en_conteneur_aucun_fichier_de_log_n_est_cree(database_url, tmp_path):
    """
    Deux propriétés d'un coup, sans écrire une ligne en base.

    Avec MTGDB_CONTAINER=1 et une DATABASE_URL locale, le run doit être refusé
    par le garde-fou — et ce refus ne doit avoir créé aucun fichier de journal.
    """
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test suppose une DATABASE_URL locale (poste de développement).")

    logs = ROOT / "logs"
    avant = {p.name for p in logs.glob("update_*.log")} if logs.exists() else set()

    env = {**os.environ, "MTGDB_CONTAINER": "1", "DATABASE_URL": database_url,
           "PYTHONIOENCODING": "utf-8"}
    env.pop("MTGDB_ALLOW_LOCAL_DB", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "update_all.py"), "--only", "game-changers"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )

    apres = {p.name for p in logs.glob("update_*.log")} if logs.exists() else set()
    assert apres == avant, "Un fichier de log a été créé alors que le disque est éphémère."

    assert proc.returncode == 1
    assert "base locale" in proc.stdout
    assert "conteneur" in proc.stdout
