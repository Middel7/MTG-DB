#!/usr/bin/env python3
"""
Remplit scryfall_card_printings.finishes sans relancer l'import complet.

POURQUOI CE SCRIPT EXISTE
`import_scryfall.py` s'arrête dès que le bulk du jour a déjà été importé : la
colonne qui vient de naître resterait donc vide jusqu'à la prochaine publication
de Scryfall, soit jusqu'à douze heures. Et forcer un import complet pour une
seule colonne ferait réécrire cartes, faces, éditions et prix qui n'ont pas
bougé.

Ce script ne lit que `finishes`, depuis le bulk DÉJÀ TÉLÉCHARGÉ, et n'écrit que
cette colonne, uniquement sur les impressions dont la valeur diffère.

Il est idempotent : un second passage ne trouve plus rien à écrire. Il ne fait
entrer aucune impression nouvelle — c'est l'import complet qui s'en charge.

Usage :
  python scripts/backfill_finishes.py             # bulk en cache, sinon telecharge
  python scripts/backfill_finishes.py --download  # force le telechargement du jour
  python scripts/backfill_finishes.py --dry-run   # compte sans ecrire
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select, update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.scryfall.bulk import HTTP_HEADERS, download_bulk_file, fetch_bulk_metadata
from mtgdb.scryfall.parsers import iter_bulk_cards

# Les helpers de typage du pipeline, réutilisés tels quels plutôt que réécrits.
# Sans le CAST qu'ils posent, un lot dont toutes les valeurs sont NULL arrive en
# `text` et PostgreSQL refuse la comparaison — défaut qui avait coûté 117 327
# cartes perdues sur un run de contrôle le 19/09. Une colonne ARRAY y est encore
# plus exposée : `text` contre `text[]` ne se compare pas davantage.
from mtgdb.scryfall.upserts import _colonne, _table_de_valeurs

RAW_DIR = ROOT / "data" / "raw" / "scryfall"

# Un UPDATE par 5 000 impressions. Le lot du pipeline vaut 500 lignes de bulk,
# calibré pour un upsert qui écrit vingt colonnes ; ici on n'en écrit qu'une.
BATCH = 5_000

log = logging.getLogger("mtgdb.backfill_finishes")


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


def ecrire_lot(session, lot: list[dict]) -> int:
    """
    Applique un lot de finitions. Retourne le nombre de lignes réellement modifiées.

    `IS DISTINCT FROM` plutôt qu'un UPDATE inconditionnel : sur un second
    passage, ou sur les impressions qu'un import complet a déjà renseignées,
    réécrire une valeur identique ne produirait que des tuples morts et du WAL.
    `IS DISTINCT FROM` traite correctement le NULL, là où `<>` laisserait passer
    toutes les lignes non encore remplies.
    """
    v = _table_de_valeurs(CardPrinting, ("scryfall_id", "finishes"), lot, "finitions")
    resultat = session.execute(
        update(CardPrinting)
        .where(CardPrinting.scryfall_id == _colonne(v, CardPrinting, "scryfall_id"))
        .where(CardPrinting.finishes.is_distinct_from(_colonne(v, CardPrinting, "finishes")))
        .values(finishes=_colonne(v, CardPrinting, "finishes"))
    )
    session.commit()
    return resultat.rowcount


def backfill(chemin: Path, dry_run: bool = False) -> tuple[int, int]:
    """Retourne (impressions vues dans le bulk, lignes modifiées en base)."""
    debut = time.time()
    with SessionLocal() as session:
        connues = {s for (s,) in session.execute(select(CardPrinting.scryfall_id))}
        log.info(f"{len(connues):,} impressions dans le catalogue")

        tampon: list[dict] = []
        lues = vues = modifiees = sans_champ = 0

        for brut in iter_bulk_cards(chemin):
            lues += 1
            scryfall_id = brut.get("id")
            finitions = brut.get("finishes")
            if not scryfall_id or scryfall_id not in connues:
                # Impression absente du catalogue : c'est l'import complet qui la
                # fera entrer, pas ce script.
                continue
            if finitions is None:
                sans_champ += 1
                continue
            vues += 1
            tampon.append({"scryfall_id": scryfall_id, "finishes": finitions})

            if len(tampon) >= BATCH:
                if not dry_run:
                    modifiees += ecrire_lot(session, tampon)
                tampon = []
                log.info(f"  {lues:>7,} lignes lues  |  {vues:>7,} impressions  "
                         f"|  {modifiees:>7,} modifiées  |  {int(time.time() - debut)}s")

        if tampon and not dry_run:
            modifiees += ecrire_lot(session, tampon)

        if sans_champ:
            log.warning(
                f"{sans_champ:,} impression(s) sans champ `finishes` dans le bulk — "
                f"elles restent NULL, c'est-à-dire « finitions inconnues »."
            )

        log.info(f"Terminé : {vues:,} impressions lues, {modifiees:,} modifiées, "
                 f"{int(time.time() - debut)}s")

        if not dry_run:
            # Ce qui reste à NULL après écriture, ce sont les impressions que ce
            # bulk ne contenait pas. Les compter ferme la boucle : un chiffre non
            # nul se lit comme « ce bulk est plus vieux que le catalogue », pas
            # comme un échec du script.
            restantes = session.scalar(
                select(func.count()).select_from(CardPrinting)
                .where(CardPrinting.finishes.is_(None))
            )
            if restantes:
                log.info(f"  {restantes:,} impression(s) restent sans finitions : "
                         f"celles absentes de ce bulk. Elles seront remplies au "
                         f"prochain import complet.")
    return vues, modifiees


def main() -> None:
    # Sur Windows, la console est en cp1252 : sans cette ligne, le premier
    # caractère accentué du journal fait lever le handler de logging.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="télécharge le bulk du jour au lieu de réutiliser le cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="compte les impressions sans rien écrire")
    args = parser.parse_args()

    if not check_connection():
        log.error("Base injoignable.")
        sys.exit(1)

    chemin = telecharger() if args.download else (dernier_bulk() or telecharger())
    log.info(f"Bulk : {chemin.name} ({chemin.stat().st_size / 1_048_576:.0f} Mo)")

    _, modifiees = backfill(chemin, dry_run=args.dry_run)
    # Ne rien avoir à écrire n'est pas un échec : c'est ce que rend un second
    # passage, et c'est la preuve que le script est idempotent.
    if args.dry_run:
        log.info("✓ Dry-run : rien n'a été écrit.")
    else:
        log.info("✓ Finitions à jour." if modifiees
                 else "✓ Rien à compléter, tout était déjà là.")


if __name__ == "__main__":
    main()
