#!/usr/bin/env python3
"""
Remplit scryfall_card_parts sans relancer l'import complet.

POURQUOI CE SCRIPT EXISTE
Le jour où le catalogue apprend une donnée qu'il ignorait — ici les jetons qu'une
carte met en jeu —, la colonne naît vide et ne se remplit qu'au prochain import
complet : deux heures d'écriture, dont l'immense majorité réécrit des cartes, des
éditions et des prix qui n'ont pas bougé.

Ce script ne lit que `all_parts`, depuis le bulk DÉJÀ TÉLÉCHARGÉ, et n'écrit que
les liaisons manquantes. Mesuré sur la base locale : 14 147 liaisons en 35 s.

Il est idempotent : `ON CONFLICT DO NOTHING` sur la contrainte d'unicité, aucune
suppression. Le relancer ne coûte que la relecture du bulk.

Il ne remplace pas l'import : il ne fait entrer aucune carte nouvelle, et ne
supprime pas une liaison que Scryfall aurait retirée — c'est `replace_parts`,
dans le pipeline, qui s'en charge à chaque run complet.

Usage :
  python scripts/backfill_card_parts.py             # bulk en cache, sinon telecharge
  python scripts/backfill_card_parts.py --download  # force le telechargement du jour
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.db.models.card import Card
from mtgdb.db.models.card_part import CardPart
from mtgdb.scryfall.bulk import HTTP_HEADERS, download_bulk_file, fetch_bulk_metadata
from mtgdb.scryfall.parsers import iter_bulk_cards, parse_part_rows

RAW_DIR = ROOT / "data" / "raw" / "scryfall"

# Un INSERT par 5 000 liaisons. Le lot du pipeline vaut 500 LIGNES DE BULK, ce qui
# n'a pas de sens ici : on ne retient qu'une ligne sur quarante.
BATCH = 5_000

log = logging.getLogger("mtgdb.backfill_parts")


def dernier_bulk() -> Path | None:
    """Le bulk le plus récent déjà sur le disque, s'il y en a un."""
    if not RAW_DIR.exists():
        return None
    fichiers = sorted(RAW_DIR.glob("*.jsonl.gz"), key=lambda p: p.stat().st_mtime)
    return fichiers[-1] if fichiers else None


def telecharger() -> Path:
    with httpx.Client(timeout=300.0, headers=HTTP_HEADERS) as client:
        uri, filename, _ = fetch_bulk_metadata(client)
        dest = RAW_DIR / filename
        if dest.exists():
            log.info(f"Bulk du jour déjà présent : {dest.name}")
            return dest
        log.info(f"Téléchargement de {filename}…")
        download_bulk_file(client, uri, dest)
    return dest


def backfill(chemin: Path) -> tuple[int, int]:
    """Écrit les liaisons manquantes. Retourne (cartes vues, liaisons écrites)."""
    debut = time.time()
    with SessionLocal() as session:
        oracle_to_id = {o: i for o, i in session.execute(select(Card.oracle_id, Card.id))}
        log.info(f"{len(oracle_to_id):,} cartes dans le catalogue")

        # Un `oracle_id` déjà traité ne l'est pas deux fois : le bulk répète
        # `all_parts` sur chacune des impressions d'une même carte, et elles se
        # comptent par dizaines pour les cartes très rééditées.
        vus: set[str] = set()
        tampon: list[dict] = []
        lignes = ecrites = 0

        for brut in iter_bulk_cards(chemin):
            lignes += 1
            oracle_id = brut.get("oracle_id")
            if not oracle_id or oracle_id in vus or not brut.get("all_parts"):
                continue
            card_id = oracle_to_id.get(oracle_id)
            if card_id is None:
                # Carte absente du catalogue : c'est l'import complet qui la fera
                # entrer, pas ce script.
                continue
            vus.add(oracle_id)
            tampon.extend(parse_part_rows(brut, card_id))

            if len(tampon) >= BATCH:
                session.execute(pg_insert(CardPart).values(tampon).on_conflict_do_nothing())
                session.commit()
                ecrites += len(tampon)
                tampon = []
                log.info(f"  {lignes:>7,} lignes lues  |  {ecrites:>7,} liaisons  "
                         f"|  {int(time.time() - debut)}s")

        if tampon:
            session.execute(pg_insert(CardPart).values(tampon).on_conflict_do_nothing())
            session.commit()
            ecrites += len(tampon)

        total = session.scalar(select(CardPart.id).limit(1))
        log.info(f"Terminé : {len(vus):,} cartes liées, {ecrites:,} liaisons proposées, "
                 f"{int(time.time() - debut)}s")
        if total is None:
            log.warning("La table reste vide : le bulk ne contenait aucun champ all_parts.")
    return len(vus), ecrites


def main() -> None:
    # La sortie est lue telle quelle par l'ecran d'administration de ManaMind,
    # qui lance ce script en sous-processus. Sur Windows, la console est en
    # cp1252 : sans cette ligne, le premier caractere accentue du journal fait
    # lever le handler de logging et le script meurt apres avoir tout ecrit.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="télécharge le bulk du jour au lieu de réutiliser le cache")
    args = parser.parse_args()

    if not check_connection():
        log.error("Base injoignable.")
        sys.exit(1)

    chemin = telecharger() if args.download else (dernier_bulk() or telecharger())
    taille = chemin.stat().st_size / 1_048_576
    log.info(f"Bulk : {chemin.name} ({taille:.0f} Mo)")

    _, ecrites = backfill(chemin)
    # Sortir en échec quand rien n'a été proposé serait faux : un second passage
    # ne trouve légitimement rien à écrire.
    log.info("✓ Jetons à jour." if ecrites else "✓ Rien à compléter, tout était déjà là.")


if __name__ == "__main__":
    main()
