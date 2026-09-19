"""
Résistance aux interruptions transitoires de la base.

Pourquoi ce module existe
-------------------------
Un run complet dure ~2 h et enchaîne ~7 000 requêtes. Supposer que la connexion
tiendra d'un bout à l'autre s'est révélé faux deux fois en trois jours :

  2026-09-17  depuis le poste : `SSL connection has been closed unexpectedly`
              après 78 min de travail ;
  2026-09-19  depuis un Cron Job Render : `the database system is not yet
              accepting connections / Consistent recovery state has not been yet
              reached` après 70 min.

Dans les deux cas, l'interruption a duré moins longtemps que le travail perdu.
Ce qu'il manquait n'était pas de la puissance, c'était d'attendre et de réessayer.

Ce qui est réessayé, et ce qui ne l'est pas
-------------------------------------------
Uniquement les erreurs de TRANSPORT : connexion refusée, coupée, base en cours
de démarrage ou de récupération. Une violation de contrainte ou une erreur de
syntaxe SQL ne guérira pas en attendant — la réessayer masquerait un vrai bug et
ferait tourner le run pendant des minutes pour rien.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

log = logging.getLogger("mtgdb.db.retry")

T = TypeVar("T")

# Délais entre tentatives, en secondes. La progression couvre ~4 min d'absence :
# un PostgreSQL qui rejoue son WAL revient généralement en moins de deux minutes,
# et au-delà de quatre il vaut mieux échouer franchement que laisser le job
# tourner des heures sur une base qui ne reviendra pas.
DEFAULT_DELAYS: tuple[int, ...] = (5, 15, 30, 60, 120)

# Signatures textuelles d'une indisponibilité passagère. psycopg2 ne fournit pas
# de code SQLSTATE exploitable pour la plupart (la connexion n'est pas établie,
# donc il n'y a pas de réponse structurée du serveur) : le message est la seule
# information disponible.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "is not yet accepting connections",      # base en cours de recovery
    "consistent recovery state",             # idem, ligne DETAIL
    "the database system is starting up",
    "the database system is shutting down",
    "in recovery mode",
    "server closed the connection unexpectedly",
    "ssl connection has been closed unexpectedly",
    "terminating connection",
    "connection refused",
    "could not connect to server",
    "could not translate host name",         # DNS transitoire lors d'un basculement
    "no connection to the server",
    "connection already closed",
    "connection timed out",
    "too many clients already",
    "canceling statement due to conflict with recovery",
)


def is_transient_error(exc: BaseException) -> bool:
    """
    True si l'erreur relève d'une indisponibilité passagère de la base.

    SQLAlchemy marque `connection_invalidated` quand il a lui-même constaté que
    la connexion était morte : c'est le signal le plus fiable, on le prend en
    premier. Sinon on retombe sur les signatures textuelles.
    """
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    if not isinstance(exc, (OperationalError, InterfaceError)):
        return False
    message = str(exc).lower()
    return any(motif in message for motif in _TRANSIENT_PATTERNS)


def retry_transient(
    operation: Callable[[], T],
    *,
    description: str,
    on_retry: Callable[[], None] | None = None,
    delays: Iterable[int] = DEFAULT_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """
    Exécute `operation`, en la rejouant tant que l'erreur est transitoire.

    `on_retry` est appelé avant chaque nouvelle tentative : c'est là qu'on remet
    la session dans un état utilisable (rollback, recyclage du pool). Il ne doit
    jamais lever — ce serait remplacer l'erreur d'origine, la seule intéressante
    pour le diagnostic, par un incident de nettoyage.

    L'opération doit être idempotente. C'est le cas de tout ce à quoi on
    l'applique ici : des upserts `ON CONFLICT DO UPDATE` et des `UPDATE`
    conditionnels, qui donnent le même résultat qu'on les joue une ou trois fois.
    """
    delais = list(delays)
    derniere = len(delais)  # nombre de tentatives supplémentaires après la première

    for tentative in range(derniere + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_transient_error(exc) or tentative == derniere:
                raise
            delai = delais[tentative]
            log.warning(
                "%s : base indisponible (%s). Nouvelle tentative dans %s s "
                "(%s/%s).",
                description,
                type(exc).__name__,
                delai,
                tentative + 1,
                derniere,
            )
            if on_retry is not None:
                try:
                    on_retry()
                except Exception as nettoyage:  # noqa: BLE001
                    log.debug("Nettoyage avant réessai impossible : %s", nettoyage)
            sleep(delai)

    raise AssertionError("inatteignable")  # pragma: no cover
