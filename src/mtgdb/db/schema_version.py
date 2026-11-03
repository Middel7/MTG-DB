"""
Le code tourne-t-il sur un schéma qui l'attend ?

Pourquoi ce module existe
-------------------------
Aucun job n'applique les migrations — choix délibéré : la base de production est
partagée avec RELIC-Trade, et un `alembic upgrade` lancé par un cron horaire y
appliquerait des changements de schéma sans que personne ne l'ait décidé.

Mais `autoDeploy: true` est actif sur les trois Cron Jobs Render. L'enchaînement
est donc :

  1. la PR est fusionnée, Render reconstruit l'image en quelques minutes ;
  2. le passage horaire suivant tourne avec le NOUVEAU code ;
  3. si la migration n'a pas été appliquée entre-temps, l'import écrit sur une
     colonne inexistante et échoue.

La fenêtre est d'une heure au mieux, de quelques secondes au pire. Elle a été
franchie deux fois à la main, avec quinze minutes de marge la première fois —
par chance, pas par conception.

Ce que ce module change
-----------------------
Il ne répare rien : il **refuse de commencer**. Un run qui s'arrête en deux
secondes sur « il manque la migration X » se diagnostique d'un coup d'œil, là
où un `UndefinedColumn` surgi au milieu de 540 000 upserts envoie chercher la
panne dans le pipeline.

La comparaison se fait sur les révisions Alembic plutôt que sur la liste des
colonnes : si la révision concorde, le schéma concorde, et le contrôle couvre
d'avance les migrations qui ne touchent pas aux colonnes (index, vues,
contraintes).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

log = logging.getLogger("mtgdb.db.schema_version")

# Table de version propre à ce dépôt. La base en héberge DEUX — celle-ci et
# `mtgtrade_alembic_version` — puisqu'elle est partagée avec RELIC-Trade.
VERSION_TABLE = "mtgdb_alembic_version"

# États possibles, du plus bénin au plus grave.
A_JOUR = "a_jour"
BASE_EN_AVANCE = "base_en_avance"
CODE_EN_AVANCE = "code_en_avance"
INDETERMINE = "indetermine"


def comparer_revisions(
    tete_code: Optional[str],
    revision_base: Optional[str],
    revisions_connues: list[str],
) -> str:
    """
    Compare ce que le code attend à ce que la base porte. Fonction pure.

    `revisions_connues` est l'historique complet du dépôt, de la tête vers la
    racine — ce que `ScriptDirectory.walk_revisions()` produit.

    Quatre verdicts :

      A_JOUR          les deux concordent ;
      CODE_EN_AVANCE  la base porte une révision ANTÉRIEURE : des migrations du
                      dépôt n'ont pas été appliquées. C'est le cas dangereux ;
      BASE_EN_AVANCE  la base porte une révision que ce code ne connaît pas.
                      Bénin : une image plus ancienne tourne contre une base
                      déjà migrée, et `ADD COLUMN` est rétrocompatible ;
      INDETERMINE     l'un des deux manque — base jamais migrée, ou dépôt sans
                      migration. On ne bloque pas sur une inconnue.
    """
    if not tete_code or not revision_base:
        return INDETERMINE
    if tete_code == revision_base:
        return A_JOUR
    if revision_base in revisions_connues:
        # La base est quelque part dans NOTRE historique, mais pas à la tête :
        # il reste des migrations à appliquer.
        return CODE_EN_AVANCE
    return BASE_EN_AVANCE


def tete_du_code(racine: Path | None = None) -> tuple[Optional[str], list[str]]:
    """
    Révision attendue par ce code, et tout son historique.

    Lue depuis `alembic/` plutôt que figée dans une constante : une constante se
    désynchronise au premier oubli, et c'est précisément l'oubli qu'on traque.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover — alembic est une dépendance du projet
        return None, []

    racine = racine or Path(__file__).resolve().parents[3]
    fichier = racine / "alembic.ini"
    if not fichier.exists():
        log.debug("alembic.ini introuvable (%s) : contrôle de schéma ignoré.", fichier)
        return None, []

    try:
        script = ScriptDirectory.from_config(Config(str(fichier)))
        return script.get_current_head(), [r.revision for r in script.walk_revisions()]
    except Exception as exc:  # noqa: BLE001
        log.debug("Historique Alembic illisible (%s) : contrôle de schéma ignoré.", exc)
        return None, []


def revision_en_base(session: Session) -> Optional[str]:
    """Révision appliquée, ou None si la table de version n'existe pas encore."""
    try:
        return session.execute(
            sa_text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608 — nom constant
        ).scalar()
    except Exception as exc:  # noqa: BLE001
        log.debug("Table %s illisible (%s).", VERSION_TABLE, exc)
        return None


def verifier_schema(
    session_factory: Callable[[], Session],
    racine: Path | None = None,
) -> tuple[str, str]:
    """
    Retourne (état, message prêt à journaliser).

    N'interrompt rien et ne lève pas : c'est l'appelant qui décide quoi faire
    d'un `CODE_EN_AVANCE`. Un contrôle qui planterait de lui-même ajouterait une
    panne là où il doit en éviter une.
    """
    tete, connues = tete_du_code(racine)
    try:
        with session_factory() as session:
            base = revision_en_base(session)
    except Exception as exc:  # noqa: BLE001
        return INDETERMINE, f"Révision en base illisible ({exc})."

    etat = comparer_revisions(tete, base, connues)

    if etat == A_JOUR:
        return etat, f"Schéma à jour (révision {base})."
    if etat == CODE_EN_AVANCE:
        manquantes = connues[:connues.index(base)] if base in connues else []
        liste = ", ".join(reversed(manquantes)) or "(indéterminées)"
        return etat, (
            f"MIGRATION MANQUANTE. Ce code attend la révision {tete}, la base est "
            f"restée en {base}. À appliquer : {liste}. "
            f"Lancer « alembic upgrade head » sur cette base AVANT de rejouer ce run."
        )
    if etat == BASE_EN_AVANCE:
        return etat, (
            f"La base porte la révision {base}, que ce code ne connaît pas "
            f"(il attend {tete}). Image plus ancienne que le schéma : sans danger, "
            f"les ajouts de colonnes étant rétrocompatibles."
        )
    return etat, (
        f"Impossible de comparer les révisions (code={tete}, base={base}) — "
        f"contrôle ignoré."
    )
