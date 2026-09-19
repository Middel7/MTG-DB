"""
Non-régression sur le verrou anti-chevauchement.

Le verrou fichier `data/.update_all.lock` a été remplacé par un verrou
consultatif PostgreSQL. Deux propriétés doivent tenir :

  - un second run voit le verrou et sort en code 2, sans rien écrire ;
  - le verrou est bien relâché à la fin du run, sinon le run suivant
    s'arrêterait indéfiniment sur un verrou fantôme.

Un verrou consultatif ne se simule pas utilement : c'est le serveur qui
l'arbitre. Ces tests ont donc besoin d'une vraie base, et se sautent sans.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mtgdb.db.lock import UPDATE_ALL_LOCK_KEY, AdvisoryLockHeld, advisory_lock

ROOT = Path(__file__).resolve().parents[1]


def test_la_cle_du_verrou_est_figee():
    # La valeur est écrite en dur dans le module, pas recalculée au démarrage :
    # deux runs d'âges différents doivent se voir. Si ce test tombe, c'est que
    # quelqu'un a changé la clé — et que les runs en cours ailleurs ne verront
    # plus les nouveaux.
    assert UPDATE_ALL_LOCK_KEY == 5713668333999869625
    assert -(2 ** 63) <= UPDATE_ALL_LOCK_KEY < 2 ** 63  # bigint accepté par PostgreSQL


def _verrou_tenu(engine, key: int) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted "
                    "AND (classid::bigint << 32) | objid::bigint = :k"
                ),
                {"k": key},
            ).scalar()
        )


@pytest.mark.integration
def test_le_verrou_est_visible_puis_relache(database_url):
    engine = create_engine(database_url)
    try:
        assert not _verrou_tenu(engine, UPDATE_ALL_LOCK_KEY), (
            "Un verrou traîne déjà : un run est-il en cours sur cette base ?"
        )
        with advisory_lock(database_url, heartbeat_seconds=0):
            assert _verrou_tenu(engine, UPDATE_ALL_LOCK_KEY)
        assert not _verrou_tenu(engine, UPDATE_ALL_LOCK_KEY)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_un_second_preneur_se_voit_refuser(database_url):
    with advisory_lock(database_url, heartbeat_seconds=0):
        with pytest.raises(AdvisoryLockHeld):
            with advisory_lock(database_url, heartbeat_seconds=0):
                pytest.fail("Le second verrou n'aurait jamais dû être accordé.")


@pytest.mark.integration
def test_le_verrou_est_relache_meme_sur_exception(database_url):
    engine = create_engine(database_url)
    try:
        with pytest.raises(ZeroDivisionError):
            with advisory_lock(database_url, heartbeat_seconds=0):
                1 / 0
        assert not _verrou_tenu(engine, UPDATE_ALL_LOCK_KEY)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_le_heartbeat_garde_la_connexion_vivante(database_url):
    # La connexion porteuse reste ouverte jusqu'à 2 h sans échanger un octet :
    # sans trafic, un NAT ou un pare-feu la couperait en silence et PostgreSQL
    # libérerait le verrou sans que personne ne s'en aperçoive.
    import time

    engine = create_engine(database_url)
    try:
        with advisory_lock(database_url, heartbeat_seconds=0.2):
            time.sleep(0.7)  # laisse passer plusieurs battements
            assert _verrou_tenu(engine, UPDATE_ALL_LOCK_KEY)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_update_all_sort_en_code_2_quand_le_verrou_est_tenu(database_url):
    """
    Le contrat de sortie du script, bout en bout.

    Le verrou est pris AVANT la première étape : le sous-processus doit donc
    ressortir immédiatement, sans avoir touché à la moindre table.
    """
    with advisory_lock(database_url, heartbeat_seconds=0):
        proc = subprocess.run(
            # --no-log-file : un test ne doit rien laisser derrière lui dans logs/.
            [sys.executable, str(ROOT / "scripts" / "update_all.py"),
             "--only", "game-changers", "--no-log-file"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    assert proc.returncode == 2, f"code {proc.returncode} au lieu de 2\n{proc.stdout}\n{proc.stderr}"
    assert "verrou" in proc.stdout.lower()
    # Aucune étape ne doit avoir démarré.
    assert "[1/1]" not in proc.stdout

@pytest.mark.integration
def test_la_connexion_du_verrou_ne_reste_pas_en_transaction(database_url):
    """
    La session porteuse doit être `idle`, jamais `idle in transaction`.

    Constaté en production le 19/09 : SQLAlchemy ouvre une transaction implicite
    au premier `SELECT 1` du heartbeat et ne la referme jamais. La session reste
    alors `idle in transaction` pendant les deux heures du run, ce qui gèle
    l'horizon de `VACUUM` : les tuples morts des 520 000 lignes réécrites ne
    peuvent plus être recyclés, et l'IO qu'on cherche à réduire empire.
    """
    import time

    engine = create_engine(database_url)
    try:
        with advisory_lock(database_url, heartbeat_seconds=0.2):
            time.sleep(0.6)  # laisse passer plusieurs battements
            with engine.connect() as conn:
                etat = conn.execute(text("""
                    SELECT a.state FROM pg_stat_activity a
                    JOIN pg_locks l ON l.pid = a.pid
                    WHERE l.locktype = 'advisory' AND l.granted
                      AND (l.classid::bigint << 32) | l.objid::bigint = :k
                """), {"k": UPDATE_ALL_LOCK_KEY}).scalar()
        assert etat is not None, "la session porteuse du verrou est introuvable"
        assert etat != "idle in transaction", (
            "la connexion du verrou laisse une transaction ouverte : "
            "elle bloquerait le VACUUM pendant toute la durée du run"
        )
    finally:
        engine.dispose()
