#!/usr/bin/env python3
"""Import du Product Catalog Cardmarket (Magic Singles)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.cardmarket import PRODUCT_CATALOG_URL
from mtgdb.cardmarket.download import download_file
from mtgdb.cardmarket.import_product_catalog import import_product_catalog
from mtgdb.db.engine import SessionLocal, check_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_cardmarket_products")

RAW_DIR = ROOT / "data" / "raw" / "cardmarket" / "product_catalog"


def main() -> None:
    if not check_connection():
        log.error("Connexion PostgreSQL impossible.")
        sys.exit(1)

    with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        with SessionLocal() as session:
            log.info("Téléchargement du Product Catalog Cardmarket…")
            file_path, import_row = download_file(
                client, session, PRODUCT_CATALOG_URL, "product_catalog_magic_singles", RAW_DIR
            )

            if file_path is None:
                log.info("Fichier non modifié — import ignoré.")
                return

            log.info("Import en base…")
            n = import_product_catalog(file_path, session, import_row)
            log.info(f"Terminé — {n:,} produits importés.")


if __name__ == "__main__":
    main()
