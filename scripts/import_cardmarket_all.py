#!/usr/bin/env python3
"""Import complet Cardmarket : Product Catalog + Price Guide + rapport de liaison."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.cardmarket import PRICE_GUIDE_URL, PRODUCT_CATALOG_URL
from mtgdb.cardmarket.download import download_file
from mtgdb.cardmarket.import_price_guide import import_price_guide
from mtgdb.cardmarket.import_product_catalog import import_product_catalog
from mtgdb.cardmarket.link_scryfall import link_scryfall
from mtgdb.db.engine import SessionLocal, check_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_cardmarket_all")

RAW_CATALOG_DIR = ROOT / "data" / "raw" / "cardmarket" / "product_catalog"
RAW_PRICE_DIR = ROOT / "data" / "raw" / "cardmarket" / "price_guide"


def main() -> None:
    if not check_connection():
        log.error("Connexion PostgreSQL impossible.")
        sys.exit(1)

    with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        with SessionLocal() as session:

            log.info("=== 1/3 Product Catalog ===")
            path, row = download_file(
                client, session, PRODUCT_CATALOG_URL, "product_catalog_magic_singles", RAW_CATALOG_DIR
            )
            if path:
                import_product_catalog(path, session, row)
            else:
                log.info("  Product Catalog non modifié — ignoré.")

            log.info("=== 2/3 Price Guide ===")
            path, row = download_file(
                client, session, PRICE_GUIDE_URL, "price_guide_magic", RAW_PRICE_DIR
            )
            if path:
                import_price_guide(path, session, row)
            else:
                log.info("  Price Guide non modifié — ignoré.")

            log.info("=== 3/3 Rapport de liaison ===")
            link_scryfall(session)


if __name__ == "__main__":
    main()
