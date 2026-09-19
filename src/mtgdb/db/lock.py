"""
Verrou anti-chevauchement porté par PostgreSQL (`pg_advisory_lock`).

Pourquoi pas un fichier
-----------------------
`data/.update_all.lock` protégeait un dossier, pas une base. Deux limites l'ont
rendu inopérant :

  - un Cron Job Render part d'un conteneur neuf à chaque run : son disque n'a
    jamais vu le verrou du run précédent, et deux machines différentes ne
    partagent aucun fichier. Pendant la transition poste → cloud, c'est
    précisément le scénario contre lequel il fallait protéger ;
  - à l'inverse, il bloquait à tort : un run local vers `manamind` et un run
    prod vers `relictrade` se disputaient le même fichier, alors qu'ils
    n'écrivent pas dans la même base. C'est ce qui imposait d'espacer
    artificiellement les créneaux du Planificateur Windows.

Un verrou consultatif PostgreSQL est porté par la base visée : il protège
exactement ce qu'il y a à protéger, quelle que soit la machine d'origine, et il
n'existe pas de verrou périmé à nettoyer — le serveur le libère dès que la
session tombe, y compris sur un crash ou une coupure réseau.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

log = logging.getLogger("mtgdb.db.lock")

# Clé du verrou de `scripts/update_all.py`, un bigint comme l'exige
# pg_advisory_lock. Valeur figée, obtenue une fois par :
#
#   int.from_bytes(hashlib.blake2b(b"mtgdb:update_all", digest_size=8).digest(),
#                  "big", signed=True)
#
# Elle est écrite en dur, et non recalculée au démarrage : une clé de verrou doit
# survivre à un changement de version de Python ou d'algorithme de hachage, sinon
# deux runs d'âges différents cesseraient silencieusement de se voir.
UPDATE_ALL_LOCK_KEY = 5713668333999869625

# TCP keepalives : la connexion qui porte le verrou reste ouverte pendant toute
# la durée du run (jusqu'à 2 h mesurées) sans échanger un octet. Un routeur, un
# NAT ou un pare-feu la couperait en silence, et PostgreSQL libérerait le verrou
# sans que personne ne s'en aperçoive.
_KEEPALIVE_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


class AdvisoryLockHeld(RuntimeError):
    """Un autre run détient déjà le verrou sur cette base."""


class _Heartbeat:
    """
    Maintient vivante la connexion porteuse du verrou.

    Sans trafic, rien ne distingue une connexion saine d'une connexion coupée :
    la perte ne serait constatée qu'à la libération, deux heures trop tard. Un
    `SELECT 1` périodique donne cette information tout de suite.
    """

    def __init__(self, conn: Connection, interval: float, on_lost: Callable[[str], None]):
        self._conn = conn
        self._interval = interval
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="mtgdb-lock-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def ping(self) -> None:
        """Exécute le ping. Le verrou d'exclusion protège de la libération concurrente."""
        with self._guard:
            if not self._stop.is_set():
                self._conn.execute(text("SELECT 1"))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.ping()
            except Exception as exc:  # noqa: BLE001 — on ne veut jamais tuer le run ici
                self._on_lost(str(exc))
                return


def _lock_lost(reason: str) -> None:
    """
    Signale la perte du verrou sans interrompre le run.

    Choix délibéré : un import à moitié appliqué est pire qu'un chevauchement.
    Les upserts sont idempotents, deux runs simultanés se recouvrent sans se
    corrompre ; en revanche, tuer un run au milieu de 520 000 impressions
    laisserait la base dans un état partiel. On journalise fort et on continue.
    """
    log.error(
        "Verrou advisory perdu en cours de run (%s). Le run CONTINUE : "
        "l'interrompre laisserait la base à moitié à jour. Un second run "
        "pourrait désormais démarrer en parallèle.",
        reason,
    )


@contextmanager
def advisory_lock(
    database_url: str,
    key: int = UPDATE_ALL_LOCK_KEY,
    *,
    heartbeat_seconds: float = 60.0,
) -> Iterator[int]:
    """
    Prend un verrou consultatif de session sur la base visée.

    Lève `AdvisoryLockHeld` si un autre run le détient déjà — l'appelant traduit
    cela en code de sortie 2. Libère le verrou à la sortie du bloc, y compris sur
    exception ; et de toute façon à la fermeture de la session côté serveur.

    `pg_try_advisory_lock` et non `pg_advisory_lock` : on veut savoir tout de
    suite qu'un run tourne déjà, pas attendre son tour pendant deux heures pour
    refaire le même travail juste après.
    """
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=_KEEPALIVE_ARGS,
        # Le verrou est lié à la SESSION : la connexion doit rester la même du
        # début à la fin. NullPool éviterait tout recyclage, mais on conserve
        # ici une connexion explicitement tenue ouverte, ce qui suffit.
    )
    conn = engine.connect()
    heartbeat: _Heartbeat | None = None
    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
        ).scalar()
        if not acquired:
            raise AdvisoryLockHeld(
                f"Un autre run détient déjà le verrou {key} sur cette base "
                f"(pg_try_advisory_lock). Il peut venir d'une autre machine."
            )

        if heartbeat_seconds > 0:
            heartbeat = _Heartbeat(conn, heartbeat_seconds, _lock_lost)
            heartbeat.start()

        yield key
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        except Exception as exc:  # noqa: BLE001
            # Sans importance : la fermeture de la session libère le verrou.
            log.debug("pg_advisory_unlock a échoué (%s) — libéré à la déconnexion.", exc)
        conn.close()
        engine.dispose()
