#!/usr/bin/env python3
"""Import complet Cardmarket : Product Catalog + Price Guide + rapport de liaison."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.cardmarket import PRICE_GUIDE_URL, PRODUCT_CATALOG_URL
from mtgdb.cardmarket.download import download_file, marquer_imports_orphelins
from mtgdb.cardmarket.import_price_guide import import_price_guide, purge_old_captures
from mtgdb.cardmarket.import_product_catalog import import_product_catalog
from mtgdb.cardmarket.link_scryfall import link_scryfall, rapport_croissance
from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.rawfiles import purge_old_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_cardmarket_all")

RAW_CATALOG_DIR = ROOT / "data" / "raw" / "cardmarket" / "product_catalog"
RAW_PRICE_DIR = ROOT / "data" / "raw" / "cardmarket" / "price_guide"

# Chaque run ajoute ~46 Mo (25 Mo de price guide + 20 Mo de catalogue). Sans purge, ces
# répertoires grossissent indéfiniment. On garde le fichier courant plus un précédent,
# de quoi comparer deux exports en cas de doute sur un import.
KEEP_DOWNLOADS = 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-captures", type=int, default=0, metavar="N",
        help=(
            "Ne conserver que les N captures de prix les plus récentes (0 = tout "
            "garder, défaut). Une capture pèse ~62 Mo : en quotidien, sans purge, "
            "la table grossit de ~23 Go/an. Les consommateurs ne lisent que la "
            "capture la plus récente. SUPPRESSION DÉFINITIVE."
        ),
    )
    args = parser.parse_args()

    if not check_connection():
        log.error("Connexion PostgreSQL impossible.")
        sys.exit(1)

    with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        with SessionLocal() as session:

            orphelins = marquer_imports_orphelins(session)
            if orphelins:
                log.warning(
                    "%d import(s) Cardmarket reste(s) 'started' marque(s) 'failed' "
                    "— processus interrompu sans finalisation.", orphelins)

            log.info("=== 1/3 Product Catalog ===")
            path, row = download_file(
                client, session, PRODUCT_CATALOG_URL,
                "product_catalog_magic_singles", RAW_CATALOG_DIR,
            )
            if path:
                import_product_catalog(path, session, row)
            else:
                log.info("  Product Catalog non modifié — ignoré.")
            purge_old_files(
                RAW_CATALOG_DIR, keep=KEEP_DOWNLOADS,
                current=path.name if path else None, logger=log,
            )

            log.info("=== 2/3 Price Guide ===")
            path, row = download_file(
                client, session, PRICE_GUIDE_URL, "price_guide_magic", RAW_PRICE_DIR
            )
            if path:
                import_price_guide(path, session, row)
            else:
                log.info("  Price Guide non modifié — ignoré.")
            purge_old_files(
                RAW_PRICE_DIR, keep=KEEP_DOWNLOADS,
                current=path.name if path else None, logger=log,
            )
            # Après l'import, pour ne jamais se retrouver sans aucune capture si
            # le téléchargement échoue.
            purge_old_captures(session, keep=args.keep_captures)

            log.info("=== 3/3 Rapport de liaison ===")
            link_scryfall(session)
            rapport_croissance(session)


if __name__ == "__main__":
    main()
