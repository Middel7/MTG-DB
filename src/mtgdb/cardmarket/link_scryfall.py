"""
Liaison Cardmarket ↔ Scryfall via cardmarket_id.
Scryfall expose directement idProduct Cardmarket dans le champ cardmarket_id
du JSON bulk data. Ce champ est stocké dans card_printings.cardmarket_id.

Le lien est donc direct : card_printings.cardmarket_id = cardmarket_products.id_product.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.db.models.cardmarket_product import CardmarketProduct

log = logging.getLogger("cardmarket.link_scryfall")


def rapport_croissance(session: Session) -> None:
    """
    Trace la taille et l'âge de l'historique des prix Cardmarket.

    La rétention de `cardmarket_price_guide_entries` n'est PAS assurée par ce
    dépôt : `purge_old_captures()` est désactivée par défaut et interdite en
    production, où c'est RELIC-Trade qui purge, par son propre script. Un choix
    défendable — l'arbitrage appartient à qui lit la table — mais qui crée une
    dépendance invisible : si ce script cesse d'être planifié, personne ici ne
    l'apprend.

    Trois lignes de journal à chaque run suffisent à rendre la dérive visible.
    La table pesait 3 366 Mo pour 51 captures au 19/09/2026, soit ~66 Mo par
    capture et ~24 Go par an en quotidien.
    """
    from sqlalchemy import text as sa_text

    stats = session.execute(sa_text("""
        SELECT count(*)                               AS lignes,
               count(DISTINCT captured_at)            AS captures,
               min(captured_at)::date                 AS plus_ancienne,
               pg_size_pretty(pg_total_relation_size('cardmarket_price_guide_entries'))
                                                      AS taille
          FROM cardmarket_price_guide_entries
    """)).one()

    log.info("")
    log.info("  Historique des prix Cardmarket")
    log.info(f"    Lignes / captures         : {stats.lignes:>10,} / {stats.captures}")
    log.info(f"    Capture la plus ancienne  : {stats.plus_ancienne}")
    log.info(f"    Taille de la table        : {stats.taille:>10}")
    if stats.captures and stats.captures > 400:
        log.warning(
            f"    {stats.captures} captures conservées : la purge de rétention "
            f"tourne-t-elle encore ? (elle appartient au projet qui LIT la table)"
        )


def link_scryfall(session: Session) -> None:
    total_printings = session.execute(
        select(func.count()).select_from(CardPrinting)
        .where(CardPrinting.cardmarket_id.isnot(None))
    ).scalar_one()

    total_products = session.execute(
        select(func.count()).select_from(CardmarketProduct)
    ).scalar_one()

    linked = session.execute(
        select(func.count()).select_from(CardPrinting)
        .join(CardmarketProduct, CardPrinting.cardmarket_id == CardmarketProduct.id_product)
    ).scalar_one()

    unlinked = total_printings - linked

    log.info("")
    log.info("=" * 50)
    log.info("  Rapport de liaison Cardmarket <-> Scryfall")
    log.info("=" * 50)
    log.info(f"  Produits Cardmarket         : {total_products:>10,}")
    log.info(f"  Impressions avec CM id      : {total_printings:>10,}")
    log.info(f"  Liens directs (id match)    : {linked:>10,}")
    log.info(f"  Sans correspondance CM      : {unlinked:>10,}")
    log.info("=" * 50)
