"""
Traçabilité des imports dans `import_runs`.

Pourquoi ce module existe
-------------------------
`import_runs` n'était alimentée que par `scripts/import_scryfall.py`, qui en
portait aussi toute la logique — ouverture, finalisation sur session neuve,
nettoyage des orphelins. Les trois autres sources n'écrivaient rien.

Conséquence mesurée : rien en base ne disait quand les tags avaient été
rafraîchis pour la dernière fois. Un Tagger indisponible pendant des mois
n'aurait laissé aucune trace consultable.

Ces fonctions sont ici, et non dans `scripts/`, pour que les quatre imports les
partagent sans se copier — et pour qu'elles soient testables sans charger un
script de 900 lignes par son chemin de fichier.

Sémantique des compteurs
------------------------
Les colonnes portent les noms de la source historique (Scryfall). Pour les
autres sources, on retient la lecture suivante, stable et documentée :

    cards_imported       nombre d'entités principales traitées avec succès
    printings_imported   nombre d'entités secondaires écrites (tags, prix…)

Changer ces noms demanderait une migration sur une base partagée avec
ManaMind_AI et RELIC-Trade : le jeu n'en vaut pas la chandelle.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from mtgdb.db.retry import retry_transient

log = logging.getLogger("mtgdb.db.runs")


def ouvrir_run(session: Session, source: str, source_file: str | None = None) -> int:
    """Crée une ligne `running` et retourne son id.

    L'id est retourné hors ORM : l'objet deviendrait inutilisable si la session
    cassait plus tard, alors que l'entier, lui, survit à tout.
    """
    run_id = session.execute(sa_text("""
        INSERT INTO import_runs (source, source_file, status, started_at,
                                 cards_imported, printings_imported, errors_count)
        VALUES (:source, :fichier, 'running', now(), 0, 0, 0)
        RETURNING id
    """), {"source": source, "fichier": source_file}).scalar_one()
    session.commit()
    return int(run_id)


def finaliser_run(
    session_factory: Callable[[], Session],
    run_id: int,
    status: str,
    *,
    cards: int = 0,
    printings: int = 0,
    errors: int = 0,
    error_message: str | None = None,
    on_retry: Callable[[], None] | None = None,
) -> bool:
    """
    Écrit le statut final dans une session NEUVE. Retourne False si même cela a échoué.

    Pourquoi ne pas réutiliser la session du run : quand le statut à écrire est
    `failed`, la cause la plus probable est une base injoignable. La session en
    cours est alors dans un état indéterminé, et un `rollback` y expire les objets
    ORM — les attributs qu'on vient d'affecter seraient rechargés depuis la base,
    donc perdus, et le commit n'écrirait rien.
    """
    def _ecrire() -> bool:
        with session_factory() as session_finale:
            session_finale.execute(sa_text("""
                UPDATE import_runs
                   SET status = :status,
                       finished_at = now(),
                       cards_imported = :cards,
                       printings_imported = :printings,
                       errors_count = :errors,
                       error_message = :message
                 WHERE id = :id
            """), {"status": status, "cards": cards, "printings": printings,
                   "errors": errors, "message": error_message, "id": run_id})
            session_finale.commit()
        return True

    try:
        return retry_transient(
            _ecrire,
            description=f"enregistrement du statut '{status}' du run #{run_id}",
            on_retry=on_retry,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Statut '%s' non enregistre pour le run #%s (%s). Il restera 'running' "
            "jusqu'a ce qu'un run ulterieur le marque orphelin.", status, run_id, exc)
        return False


def marquer_runs_orphelins(session: Session, source: str, older_than_hours: int = 6) -> int:
    """
    Marque `failed` les runs restés `running` d'un processus qui n'existe plus.

    Un run tué brutalement (conteneur arrêté, base injoignable au moment d'écrire
    son statut) laisse une ligne `running` éternelle : la supervision croit un
    import en cours et l'historique devient illisible.

    Le seuil n'est qu'une sécurité — c'est le verrou advisory qui garantit
    réellement qu'aucun autre run ne tourne.
    """
    resultat = session.execute(sa_text("""
        UPDATE import_runs
           SET status = 'failed',
               finished_at = now(),
               error_message = COALESCE(
                   error_message,
                   'Run orphelin : processus interrompu avant d''avoir pu écrire son statut. '
                   'Marqué par un run ultérieur.')
         WHERE source = :source
           AND status = 'running'
           AND started_at < now() - make_interval(hours => :heures)
    """), {"source": source, "heures": older_than_hours})
    session.commit()
    return resultat.rowcount


def dernier_run_reussi(session: Session, source: str) -> Optional[str]:
    """Horodatage du dernier run `success` d'une source, ou None. Sert au diagnostic."""
    return session.execute(sa_text("""
        SELECT max(finished_at)::text FROM import_runs
         WHERE source = :source AND status = 'success'
    """), {"source": source}).scalar()
