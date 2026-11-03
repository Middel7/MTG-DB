"""
Non-régression : le verrou protège aussi les appels directs aux scripts.

Le verrou ne couvrait que `update_all.py`. Les scripts d'import appelés
directement — ce qu'on fait en dépannage — écrivaient sans lui.

Constaté le 03/11/2026 : un `import_scryfall.py --force` lancé à la main depuis
le poste a tourné deux heures en parallèle du cron horaire. Les deux écrivaient
sur `scryfall_card_printings` ; le run manuel s'est achevé sur **43 000 erreurs**
et le statut `partial`. Aucune donnée corrompue — les upserts sont idempotents —
mais deux heures pour un résultat qu'un seul run aurait produit proprement.

Un verrou qui ne protège que celui qui pense à le prendre ne protège rien.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mtgdb.db.lock import VARIABLE_VERROU_DETENU, advisory_lock, verrou_si_autonome

ROOT = Path(__file__).resolve().parents[1]

#: Les scripts qui écrivent dans le catalogue. `import_game_changers.py` en est
#: absent : il ne touche que 53 lignes, par un UPDATE unique et atomique.
SCRIPTS_PROTEGES = (
    "import_scryfall.py",
    "import_cardmarket_all.py",
    "import_tagger_tags.py",
)


class TestSignalDOrchestration:
    def test_le_verrou_est_saute_quand_l_orchestrateur_le_detient(self, monkeypatch):
        """
        Sans cette échappatoire, `update_all.py` se bloquerait lui-même : il
        détient le verrou, et chacun de ses sous-processus tenterait de le
        reprendre dans une AUTRE session PostgreSQL.
        """
        monkeypatch.setenv(VARIABLE_VERROU_DETENU, "1")
        with verrou_si_autonome("postgresql://inexistant/attrape") as cle:
            assert cle is None, "aucun verrou ne doit être pris"

    def test_sans_le_signal_le_verrou_est_bien_tente(self, monkeypatch):
        """L'échappatoire ne doit pas s'activer toute seule : sans la variable,
        on tente la connexion — et on échoue ici, faute de base."""
        monkeypatch.delenv(VARIABLE_VERROU_DETENU, raising=False)
        with pytest.raises(Exception):
            with verrou_si_autonome("postgresql://inexistant:1/attrape"):
                pytest.fail("la connexion n'aurait pas dû aboutir")


@pytest.mark.parametrize("script", SCRIPTS_PROTEGES)
def test_chaque_script_d_import_prend_le_verrou(script):
    """
    Garde-fou statique : un script d'import ajouté plus tard, ou dont on
    retirerait le verrou, se verrait ici — sans base ni réseau.
    """
    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "verrou_si_autonome" in source, (
        f"{script} n'appelle pas verrou_si_autonome() : lancé à la main pendant "
        f"un run du cron, il écrirait sur les mêmes tables."
    )


@pytest.mark.integration
def test_un_script_direct_refuse_de_tourner_si_le_verrou_est_tenu(
    database_url, verrou_libre):
    """
    Le scénario exact du 03/11, rejoué : le verrou est tenu, et un script lancé
    directement doit sortir en code 2 plutôt que d'écrire en parallèle.

    `import_cardmarket_all.py` sert de témoin : c'est le plus rapide des trois
    à rendre la main.
    """
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONIOENCODING": "utf-8"}
    env.pop(VARIABLE_VERROU_DETENU, None)

    with advisory_lock(database_url, heartbeat_seconds=0):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "import_cardmarket_all.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )

    assert proc.returncode == 2, (
        f"code {proc.returncode} au lieu de 2 — le script a tourné malgré le verrou\n"
        f"{proc.stdout[-500:]}"
    )
