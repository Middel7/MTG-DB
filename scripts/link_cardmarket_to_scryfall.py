#!/usr/bin/env python3
"""Rapport de liaison Cardmarket ↔ Scryfall via cardmarket_id."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.cardmarket.link_scryfall import link_scryfall
from mtgdb.db.engine import SessionLocal, check_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("link_cardmarket_to_scryfall")


def main() -> None:
    if not check_connection():
        log.error("Connexion PostgreSQL impossible.")
        sys.exit(1)

    with SessionLocal() as session:
        link_scryfall(session)


if __name__ == "__main__":
    main()
