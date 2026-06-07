"""
Liaison Cardmarket ↔ Scryfall via cardmarket_id.
Scryfall expose directement idProduct Cardmarket dans le champ cardmarket_id
du JSON bulk data. Ce champ est stocké dans card_printings.cardmarket_id.

Le lien est donc direct : card_printings.cardmarket_id = cardmarket_products.id_product.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.db.models.cardmarket_product import CardmarketProduct

log = logging.getLogger("cardmarket.link_scryfall")


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
