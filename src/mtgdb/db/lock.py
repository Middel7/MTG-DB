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
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from mtgdb.db.retry import is_transient_error
from mtgdb.runtime import env_flag

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

# Intervalle entre deux tentatives quand `attente_max_s` est demandé.
_INTERVALLE_SONDAGE_S = 10.0

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


class _LockHolder:
    """
    Porte le verrou : une connexion dédiée, maintenue vivante, reprise si besoin.

    La connexion est en AUTOCOMMIT. Ce n'est pas un détail de style : sans lui,
    SQLAlchemy ouvre une transaction implicite au premier `SELECT` et ne la
    referme jamais. La session apparaît alors `idle in transaction` pendant les
    deux heures du run, ce qui gèle l'horizon de `VACUUM` — sur un import qui
    réécrit 520 000 lignes, les tuples morts s'accumulent sans pouvoir être
    recyclés, et l'IO que l'on cherche à réduire empire. Un verrou consultatif
    de session survit très bien à l'absence de transaction : c'est exactement ce
    qui le distingue de `pg_advisory_xact_lock`.
    """

    def __init__(self, engine: Engine, key: int):
        self._engine = engine
        self._key = key
        self._conn: Connection | None = None
        self._guard = threading.Lock()

    def try_acquire(self) -> bool:
        conn = self._engine.connect()
        acquis = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": self._key}).scalar())
        if acquis:
            self._conn = conn
        else:
            conn.close()
        return acquis

    def ping(self) -> None:
        """Vérifie que la connexion porteuse est toujours vivante."""
        with self._guard:
            if self._conn is not None:
                self._conn.execute(text("SELECT 1"))

    def reacquire(self) -> bool:
        """
        Reprend le verrou après une coupure. False si un autre l'a pris entre-temps.

        Utile parce qu'une interruption de la base est désormais un incident dont
        le run se relève (voir `mtgdb.db.retry`) : si le run survit, son verrou
        doit survivre aussi, sinon la protection disparaît en silence pour le
        reste des deux heures.
        """
        with self._guard:
            ancienne, self._conn = self._conn, None
            if ancienne is not None:
                try:
                    ancienne.close()
                except Exception:  # noqa: BLE001 — elle est déjà morte
                    pass
            self._engine.dispose()
            return self.try_acquire()

    def release(self) -> None:
        with self._guard:
            conn, self._conn = self._conn, None
            if conn is None:
                return
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
            except Exception as exc:  # noqa: BLE001
                # Sans importance : la fermeture de la session libère le verrou.
                log.debug("pg_advisory_unlock a échoué (%s) — libéré à la déconnexion.", exc)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class _Heartbeat:
    """
    Maintient vivante la connexion porteuse du verrou.

    Sans trafic, rien ne distingue une connexion saine d'une connexion coupée :
    la perte ne serait constatée qu'à la libération, deux heures trop tard. Un
    `SELECT 1` périodique donne cette information tout de suite.
    """

    def __init__(self, holder: _LockHolder, interval: float):
        self._holder = holder
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mtgdb-lock-heartbeat",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._holder.ping()
                continue
            except Exception as exc:  # noqa: BLE001
                if not is_transient_error(exc):
                    log.error("Heartbeat du verrou interrompu (%s) — surveillance arrêtée.", exc)
                    return
                log.warning("Connexion du verrou perdue (%s). Tentative de reprise...", exc)

            try:
                repris = self._holder.reacquire()
            except Exception as exc:  # noqa: BLE001
                log.warning("Reprise du verrou impossible pour l'instant (%s).", exc)
                continue

            if repris:
                log.info("Verrou repris après la coupure.")
            else:
                # On ne tue pas le run : un import à moitié appliqué est pire
                # qu'un chevauchement. Les upserts sont idempotents, deux runs
                # simultanés se recouvrent sans se corrompre, alors qu'un arrêt
                # au milieu de 520 000 impressions laisse la base incomplète.
                log.error(
                    "Verrou perdu ET repris par un autre run. Celui-ci CONTINUE : "
                    "l'interrompre laisserait la base a moitie a jour. Deux runs "
                    "ecrivent peut-etre en parallele."
                )
                return


def _acquerir(holder: "_LockHolder", key: int, attente_max_s: float) -> bool:
    """
    Tente de prendre le verrou, en réessayant jusqu'à `attente_max_s`.

    Sondage toutes les 10 s plutôt que `pg_advisory_lock` bloquant : une attente
    côté serveur immobiliserait une connexion sans qu'on puisse la borner ni la
    journaliser, et un run qui semble figé sans rien dire est le pire des cas
    pour qui lit les logs à distance.
    """
    if holder.try_acquire():
        return True
    if attente_max_s <= 0:
        return False

    limite = time.monotonic() + attente_max_s
    log.warning(
        "Verrou %s déjà détenu — nouvelle tentative pendant %d s au plus.",
        key, int(attente_max_s))
    while time.monotonic() < limite:
        time.sleep(min(_INTERVALLE_SONDAGE_S, max(0.0, limite - time.monotonic())))
        if holder.try_acquire():
            log.info("Verrou obtenu après attente.")
            return True
    return False


# Posée par `scripts/update_all.py` dans l'environnement de ses sous-processus.
# Sans elle, chaque script d'import prendrait le verrou que l'orchestrateur
# détient déjà, et se bloquerait lui-même.
VARIABLE_VERROU_DETENU = "MTGDB_VERROU_DETENU"


@contextmanager
def verrou_si_autonome(
    database_url: str,
    *,
    attente_max_s: float = 0.0,
) -> Iterator[int | None]:
    """
    Prend le verrou, sauf si l'orchestrateur le détient déjà pour nous.

    POURQUOI CE DÉTOUR
    Le verrou ne protégeait que `update_all.py`. Les quatre scripts d'import,
    appelés directement — ce que l'on fait en dépannage —, écrivaient sans lui.

    Constaté le 03/11 : un `import_scryfall.py --force` lancé à la main depuis
    le poste a tourné pendant deux heures en parallèle du cron horaire. Les deux
    écrivaient sur `scryfall_card_printings` ; le run manuel s'est terminé avec
    **43 000 erreurs** et le statut `partial`. Aucune donnée corrompue, les
    upserts étant idempotents, mais deux heures pour un résultat qu'un seul run
    aurait produit proprement.

    Un verrou qui ne protège que celui qui pense à le prendre ne protège rien.
    """
    if env_flag(VARIABLE_VERROU_DETENU):
        # L'orchestrateur l'a pris avant de nous lancer : le reprendre ici
        # échouerait, puisque c'est une autre session PostgreSQL.
        yield None
        return
    with advisory_lock(database_url, attente_max_s=attente_max_s) as cle:
        yield cle


@contextmanager
def advisory_lock(
    database_url: str,
    key: int = UPDATE_ALL_LOCK_KEY,
    *,
    heartbeat_seconds: float = 60.0,
    attente_max_s: float = 0.0,
) -> Iterator[int]:
    """
    Prend un verrou consultatif de session sur la base visée.

    Lève `AdvisoryLockHeld` si un autre run le détient déjà — l'appelant traduit
    cela en code de sortie 2. Libère le verrou à la sortie du bloc, y compris sur
    exception ; et de toute façon à la fermeture de la session côté serveur.

    `pg_try_advisory_lock` et non `pg_advisory_lock` : on veut savoir tout de
    suite qu'un run tourne déjà, pas attendre son tour pendant deux heures pour
    refaire le même travail juste après.

    `attente_max_s` NUANCE CELA POUR LES RUNS RARES
    --------------------------------------------
    Abandonner tout de suite est le bon comportement d'un run qui repasse dans
    l'heure : le travail sera fait au passage suivant. C'est le mauvais pour un
    run HEBDOMADAIRE, qui perd alors sa semaine.

    Constaté en production : le job de tags (dimanche 05:00 UTC) et le job de
    catalogue (toutes les heures, donc 05:00 aussi) démarrent à la même seconde.
    Celui qui perd la course sort en code 2 — que Render rapporte comme un échec.
    Les tags n'ont plus été rafraîchis du 04/10 au 03/11.

    Avec `attente_max_s`, le second réessaie posément au lieu de renoncer. Un
    passage de catalogue à vide dure ~40 s, un import complet ~18 min : quelques
    minutes d'attente suffisent à couvrir les deux.
    """
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=_KEEPALIVE_ARGS,
        isolation_level="AUTOCOMMIT",
    )
    holder = _LockHolder(engine, key)
    heartbeat: _Heartbeat | None = None
    try:
        if not _acquerir(holder, key, attente_max_s):
            attendu = f" après {int(attente_max_s)} s d'attente" if attente_max_s else ""
            raise AdvisoryLockHeld(
                f"Un autre run détient déjà le verrou {key} sur cette base"
                f"{attendu} (pg_try_advisory_lock). Il peut venir d'une autre machine."
            )

        if heartbeat_seconds > 0:
            heartbeat = _Heartbeat(holder, heartbeat_seconds)
            heartbeat.start()

        yield key
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        holder.release()
        engine.dispose()
