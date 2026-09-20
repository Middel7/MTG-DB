"""
Suivi de ce que les sources amont publient.

`import_runs` dit quand MTG-DB a été mis à jour. Ce module dit quand la source
a publié — y compris pour une version qu'on n'a pas encore absorbée, qui est
précisément le cas où l'on veut être alerté.

Principe de prudence
--------------------
Aucune fonction d'ici ne doit faire échouer un import. C'est de la
**traçabilité**, pas de la donnée métier : perdre une ligne de suivi est sans
gravité, perdre un import de 540 000 impressions ne l'est pas.

Deux conséquences dans l'écriture de ce module :

1. chaque écriture ouvre sa **propre session**, jamais celle de l'appelant. Un
   `commit()` glissé au milieu d'une transaction en cours validerait son travail
   à demi et expirerait ses objets ORM — `download_file()` construit justement
   sa ligne `cardmarket_import_files` en plusieurs temps ;
2. toute exception est avalée avec un avertissement. Une base momentanément
   indisponible fait perdre une ligne de suivi, pas un import de deux heures.
"""
from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from mtgdb.db.engine import SessionLocal

log = logging.getLogger("mtgdb.db.publications")

SOURCE_SCRYFALL_BULK = "scryfall_bulk"
SOURCE_CARDMARKET_PRICE_GUIDE = "cardmarket_price_guide"
SOURCE_CARDMARKET_PRODUCT_CATALOG = "cardmarket_product_catalog"

# Correspondance entre le `file_type` de cardmarket_import_files et la source de
# suivi. Une table plutôt qu'une concaténation de chaînes : si Cardmarket ajoute
# un export, l'oubli se voit ici au lieu de produire une source fantôme.
SOURCES_PAR_FILE_TYPE = {
    "price_guide_magic": SOURCE_CARDMARKET_PRICE_GUIDE,
    "product_catalog_magic_singles": SOURCE_CARDMARKET_PRODUCT_CATALOG,
}


def parser_date_http(valeur: str | None) -> Optional[datetime]:
    """
    Convertit un en-tête HTTP `Last-Modified` en datetime aware.

    Cardmarket renvoie « Sun, 20 Sep 2026 00:42:36 GMT ». Stocké tel quel dans
    `cardmarket_import_files.last_modified`, qui est une colonne `text`, il
    n'est ni triable ni soustrayable — d'où cette conversion avant écriture
    dans le suivi.

    Retourne None sur une valeur absente ou illisible : une publication sans
    date reste plus utile qu'une publication non enregistrée.
    """
    if not valeur:
        return None
    try:
        return parsedate_to_datetime(valeur)
    except (TypeError, ValueError) as exc:
        log.debug("Date HTTP illisible (%r) : %s", valeur, exc)
        return None


def enregistrer_publication(
    source: str,
    version: str,
    published_at: datetime | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """
    Note qu'une version a été publiée par la source. Idempotent.

    Appelé à CHAQUE passage du pipeline, y compris quand la version est déjà
    connue : c'est la contrainte d'unicité (source, version) qui absorbe les
    répétitions. Un run horaire ne crée donc une ligne que les deux fois par
    jour où Scryfall publie réellement.

    Retourne True si une nouvelle publication vient d'être découverte.
    """
    if not version:
        return False

    def _ecrire(session: Session) -> bool:
        # DO UPDATE et non DO NOTHING : revoir une version deja connue n'est pas
        # un evenement, mais c'est la preuve que la veille tourne. `xmax = 0`
        # distingue l'insertion de la mise a jour — PostgreSQL ne le dit pas
        # autrement sur un upsert.
        resultat = session.execute(sa_text(
            "INSERT INTO mtgdb_source_publications "
            "       (source, version, published_at, last_seen_at) "
            "VALUES (:source, :version, :publiee, now()) "
            "ON CONFLICT (source, version) DO UPDATE "
            "   SET last_seen_at = now(), "
            "       published_at = COALESCE(mtgdb_source_publications.published_at, "
            "                               EXCLUDED.published_at) "
            "RETURNING (xmax = 0) AS insertion"
        ), {"source": source, "version": version, "publiee": published_at})
        ligne = resultat.first()
        nouvelle = bool(ligne and ligne[0])
        session.commit()
        if nouvelle:
            quand = published_at.strftime("%Y-%m-%d %H:%M UTC") if published_at else "date inconnue"
            log.info("  Nouvelle publication %s : %s (%s)", source, version, quand)
        return nouvelle

    return _dans_sa_propre_session(
        _ecrire, f"enregistrement de la publication {source}/{version}", session_factory)


def marquer_publication_importee(
    source: str,
    version: str,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """
    Marque une version comme absorbée. Sans effet si elle l'était déjà.

    `imported_at IS NULL` dans la clause : un réimport forcé (`--force`) ne doit
    pas réécrire la date de première absorption, qui est celle qui mesure la
    réactivité du pipeline.
    """
    if not version:
        return False

    def _ecrire(session: Session) -> bool:
        resultat = session.execute(sa_text(
            "UPDATE mtgdb_source_publications "
            "   SET imported_at = now() "
            " WHERE source = :source AND version = :version AND imported_at IS NULL"
        ), {"source": source, "version": version})
        session.commit()
        return resultat.rowcount > 0

    return _dans_sa_propre_session(
        _ecrire, f"marquage importé de {source}/{version}", session_factory)


def _dans_sa_propre_session(
    operation: Callable[[Session], bool],
    description: str,
    session_factory: Callable[[], Session] | None,
) -> bool:
    """
    Exécute une écriture de suivi dans une session dédiée, sans jamais lever.

    La session est ouverte ici et refermée aussitôt : l'appelant est au milieu de
    sa propre transaction, qui ne doit être ni validée ni invalidée par une
    écriture de traçabilité.
    """
    fabrique = session_factory or SessionLocal
    if fabrique is None:
        log.debug("%s ignoré : aucune connexion configurée.", description)
        return False
    try:
        with fabrique() as session:
            return operation(session)
    except Exception as exc:  # noqa: BLE001 — le suivi ne doit jamais tuer un import
        log.warning("%s impossible : %s", description, exc)
        return False


def lire_fraicheur(session: Session) -> list[dict]:
    """Retourne le contenu de la vue `mtgdb_fraicheur_sources`, une ligne par source."""
    lignes = session.execute(sa_text("SELECT * FROM mtgdb_fraicheur_sources")).mappings()
    return [dict(ligne) for ligne in lignes]


def historique_publications(session: Session, limite: int = 30) -> list[dict]:
    """
    Les dernières publications connues, toutes sources confondues.

    Répond à « quand la source a-t-elle proposé une mise à jour, et quand
    l'avons-nous prise ? » sur la durée, là où la vue ne donne que l'instantané.
    """
    lignes = session.execute(sa_text(
        "SELECT source, version, published_at, detected_at, imported_at, "
        "       imported_at - COALESCE(published_at, detected_at) AS delai_absorption "
        "  FROM mtgdb_source_publications "
        " ORDER BY COALESCE(published_at, detected_at) DESC, id DESC "
        " LIMIT :limite"
    ), {"limite": limite}).mappings()
    return [dict(ligne) for ligne in lignes]
