#!/usr/bin/env python3
"""
Import des cartes Magic: The Gathering depuis Scryfall bulk data vers PostgreSQL.

Flux :
  1. GET https://api.scryfall.com/bulk-data  → download_uri du fichier default_cards
  2. Téléchargement → data/raw/scryfall/<filename>
  3. GET https://api.scryfall.com/sets       → upsert dans mtg_sets (FK obligatoire)
  4. Parsing streaming ijson → batches de 500 cartes :
       cards / card_faces / card_printings / card_prices
  5. Mise à jour import_runs (début, fin, compteurs, erreurs)

Usage :
  python scripts/import_scryfall.py
  python scripts/import_scryfall.py --force      # retélécharge même si fichier existant
  python scripts/import_scryfall.py --dry-run    # parse et compte sans toucher la base
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import ijson
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection
from mtgdb.db.models.card import Card, normalize_card_name
from mtgdb.db.models.card_face import CardFace
from mtgdb.db.models.card_price import CardPrice
from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.db.models.import_run import ImportRun
from mtgdb.db.models.mtg_set import MtgSet

BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
SETS_URL = "https://api.scryfall.com/sets"
RAW_DIR = ROOT / "data" / "raw" / "scryfall"
BATCH_SIZE = 500
HTTP_HEADERS = {"User-Agent": "MTG-DB/1.0 (educational project)"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_scryfall")


# ══════════════════════════════════════════════════════════════════════════════
# 1. TÉLÉCHARGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_bulk_metadata(client: httpx.Client) -> tuple[str, str, datetime]:
    resp = client.get(BULK_DATA_URL)
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("type") == "default_cards":
            updated_at = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
            uri: str = item["download_uri"]
            filename = uri.rsplit("/", 1)[-1]
            return uri, filename, updated_at
    raise ValueError("Type 'default_cards' introuvable dans l'API bulk-data Scryfall.")


def download_bulk_file(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0)) or None
        with (
            open(dest, "wb") as f,
            tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as bar,
        ):
            for chunk in resp.iter_bytes(chunk_size=65_536):
                f.write(chunk)
                bar.update(len(chunk))


# ══════════════════════════════════════════════════════════════════════════════
# 2. IMPORT DES ÉDITIONS
# ══════════════════════════════════════════════════════════════════════════════

def import_sets(client: httpx.Client, session: Session) -> int:
    resp = client.get(SETS_URL)
    resp.raise_for_status()
    sets_data = resp.json().get("data", [])
    rows: list[dict] = []
    for s in sets_data:
        released = None
        if raw_date := s.get("released_at"):
            try:
                released = date.fromisoformat(raw_date)
            except ValueError:
                pass
        rows.append({
            "code": s["code"],
            "name": s["name"],
            "set_type": s.get("set_type"),
            "released_at": released,
            "block": s.get("block"),
            "parent_set_code": s.get("parent_set_code"),
            "card_count": s.get("card_count"),
            "icon_svg_uri": s.get("icon_svg_uri"),
        })
    if not rows:
        return 0
    stmt = pg_insert(MtgSet).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "name": stmt.excluded.name,
            "set_type": stmt.excluded.set_type,
            "released_at": stmt.excluded.released_at,
            "block": stmt.excluded.block,
            "parent_set_code": stmt.excluded.parent_set_code,
            "card_count": stmt.excluded.card_count,
            "icon_svg_uri": stmt.excluded.icon_svg_uri,
        },
    )
    session.execute(stmt)
    session.commit()
    return len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# 3. PARSEURS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_card_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    oracle_id = raw.get("oracle_id")
    if not oracle_id:
        return None
    name = raw.get("name", "")
    legalities = raw.get("legalities") or {}
    mana_cost = raw.get("mana_cost") or None
    if not mana_cost:
        faces = raw.get("card_faces") or []
        if faces:
            mana_cost = faces[0].get("mana_cost") or None
    return {
        "oracle_id": oracle_id,
        "name": name,
        "normalized_name": normalize_card_name(name),
        "mana_cost": mana_cost,
        "mana_value": raw.get("cmc"),
        "type_line": raw.get("type_line"),
        "oracle_text": raw.get("oracle_text"),
        "power": raw.get("power"),
        "toughness": raw.get("toughness"),
        "loyalty": raw.get("loyalty"),
        "defense": raw.get("defense"),
        "colors": raw.get("colors") or [],
        "color_identity": raw.get("color_identity") or [],
        "keywords": raw.get("keywords") or [],
        "legal_commander": legalities.get("commander") == "legal",
        "edhrec_rank": raw.get("edhrec_rank"),
    }


def _parse_face_rows(raw: dict[str, Any], card_id: int) -> list[dict[str, Any]]:
    faces_data = raw.get("card_faces") or []
    rows = []
    for face in faces_data:
        img = face.get("image_uris") or {}
        rows.append({
            "card_id": card_id,
            "face_name": face.get("name", ""),
            "mana_cost": face.get("mana_cost") or None,
            "type_line": face.get("type_line"),
            "oracle_text": face.get("oracle_text"),
            "power": face.get("power"),
            "toughness": face.get("toughness"),
            "loyalty": face.get("loyalty"),
            "defense": face.get("defense"),
            "colors": face.get("colors") or [],
            "image_small": img.get("small"),
            "image_normal": img.get("normal"),
            "image_large": img.get("large"),
        })
    return rows


def _parse_printing_row(raw: dict[str, Any], card_id: int) -> dict[str, Any]:
    img = raw.get("image_uris") or {}
    if not img:
        faces = raw.get("card_faces") or []
        if faces:
            img = faces[0].get("image_uris") or {}
    released_at = None
    if raw_date := raw.get("released_at"):
        try:
            released_at = date.fromisoformat(raw_date)
        except ValueError:
            pass
    return {
        "scryfall_id": raw["id"],
        "oracle_id": raw.get("oracle_id", ""),
        "card_id": card_id,
        "set_code": raw.get("set"),
        "collector_number": raw.get("collector_number"),
        "lang": raw.get("lang"),
        "rarity": raw.get("rarity"),
        "released_at": released_at,
        "artist": raw.get("artist"),
        "border_color": raw.get("border_color"),
        "frame": raw.get("frame"),
        "full_art": bool(raw.get("full_art")),
        "promo": bool(raw.get("promo")),
        "reprint": bool(raw.get("reprint")),
        "digital": bool(raw.get("digital")),
        "image_small": img.get("small"),
        "image_normal": img.get("normal"),
        "image_large": img.get("large"),
        "scryfall_uri": raw.get("scryfall_uri"),
    }


def _parse_price_rows(prices: dict[str, Any], printing_id: int, today: date) -> list[dict[str, Any]]:
    rows = []
    candidates = [
        ("eur", "regular", prices.get("eur")),
        ("eur", "foil",    prices.get("eur_foil")),
        ("usd", "regular", prices.get("usd")),
        ("usd", "foil",    prices.get("usd_foil")),
        ("tix", "regular", prices.get("tix")),
    ]
    for currency, price_type, price_str in candidates:
        if not price_str:
            continue
        try:
            rows.append({
                "printing_id": printing_id,
                "source": "scryfall",
                "currency": currency,
                "price_type": price_type,
                "price": float(price_str),
                "date": today,
            })
        except (ValueError, TypeError):
            pass
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 4. UPSERTS
# ══════════════════════════════════════════════════════════════════════════════

def _upsert_cards(session: Session, rows: list[dict]) -> dict[str, int]:
    stmt = pg_insert(Card).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["oracle_id"],
        set_={
            "name":             stmt.excluded.name,
            "normalized_name":  stmt.excluded.normalized_name,
            "mana_cost":        stmt.excluded.mana_cost,
            "mana_value":       stmt.excluded.mana_value,
            "type_line":        stmt.excluded.type_line,
            "oracle_text":      stmt.excluded.oracle_text,
            "power":            stmt.excluded.power,
            "toughness":        stmt.excluded.toughness,
            "loyalty":          stmt.excluded.loyalty,
            "defense":          stmt.excluded.defense,
            "colors":           stmt.excluded.colors,
            "color_identity":   stmt.excluded.color_identity,
            "keywords":         stmt.excluded.keywords,
            "legal_commander":  stmt.excluded.legal_commander,
            "edhrec_rank":      stmt.excluded.edhrec_rank,
            "updated_at":       func.now(),
        },
    )
    session.execute(stmt)
    oracle_ids = [r["oracle_id"] for r in rows]
    result = session.execute(
        select(Card.id, Card.oracle_id).where(Card.oracle_id.in_(oracle_ids))
    )
    return {row.oracle_id: row.id for row in result}


def _replace_faces(session: Session, face_rows: list[dict], card_ids: list[int]) -> None:
    session.execute(delete(CardFace).where(CardFace.card_id.in_(card_ids)))
    if face_rows:
        session.execute(pg_insert(CardFace).values(face_rows))


def _upsert_printings(session: Session, rows: list[dict]) -> dict[str, int]:
    update_cols = [
        "oracle_id", "card_id", "set_code", "collector_number", "lang",
        "rarity", "released_at", "artist", "border_color", "frame",
        "full_art", "promo", "reprint", "digital",
        "image_small", "image_normal", "image_large", "scryfall_uri",
    ]
    stmt = pg_insert(CardPrinting).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["scryfall_id"],
        set_={col: getattr(stmt.excluded, col) for col in update_cols},
    )
    session.execute(stmt)
    scryfall_ids = [r["scryfall_id"] for r in rows]
    result = session.execute(
        select(CardPrinting.id, CardPrinting.scryfall_id)
        .where(CardPrinting.scryfall_id.in_(scryfall_ids))
    )
    return {row.scryfall_id: row.id for row in result}


def _insert_prices(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(CardPrice).values(rows)
    stmt = stmt.on_conflict_do_nothing()
    session.execute(stmt)


# ══════════════════════════════════════════════════════════════════════════════
# 5. TRAITEMENT PAR BATCH
# ══════════════════════════════════════════════════════════════════════════════

def _flush_batch(
    session: Session,
    card_rows: list[dict],
    raw_cards: list[dict[str, Any]],
    today: date,
) -> tuple[int, int]:
    seen: dict[str, int] = {}
    for i, row in enumerate(card_rows):
        seen[row["oracle_id"]] = i
    dedup_idx = sorted(seen.values())
    card_rows = [card_rows[i] for i in dedup_idx]
    raw_cards = [raw_cards[i] for i in dedup_idx]

    seen_sid: dict[str, int] = {}
    for i, raw in enumerate(raw_cards):
        seen_sid[raw["id"]] = i
    raw_cards = [raw_cards[i] for i in sorted(seen_sid.values())]

    oracle_to_id = _upsert_cards(session, card_rows)

    face_rows: list[dict] = []
    printing_rows: list[dict] = []
    card_ids_with_faces: list[int] = []
    raw_prices: dict[str, dict] = {}

    for raw in raw_cards:
        oracle_id = raw.get("oracle_id")
        card_id = oracle_to_id.get(oracle_id)
        if card_id is None:
            continue
        faces = _parse_face_rows(raw, card_id)
        if faces:
            face_rows.extend(faces)
            card_ids_with_faces.append(card_id)
        printing_rows.append(_parse_printing_row(raw, card_id))
        raw_prices[raw["id"]] = raw.get("prices") or {}

    if card_ids_with_faces:
        _replace_faces(session, face_rows, card_ids_with_faces)

    scryfall_to_printing_id = _upsert_printings(session, printing_rows)

    price_rows: list[dict] = []
    for scryfall_id, prices_dict in raw_prices.items():
        pid = scryfall_to_printing_id.get(scryfall_id)
        if pid is not None:
            price_rows.extend(_parse_price_rows(prices_dict, pid, today))
    _insert_prices(session, price_rows)

    session.commit()
    return len(card_rows), len(printing_rows)


def import_cards(file_path: Path, session: Session) -> tuple[int, int, int]:
    today = date.today()
    cards_imported = 0
    printings_imported = 0
    errors_count = 0
    card_rows_buf: list[dict] = []
    raw_cards_buf: list[dict] = []

    file_size_mb = file_path.stat().st_size / 1_048_576
    log.info(f"Fichier : {file_path.name} ({file_size_mb:.0f} Mo)")

    with open(file_path, "rb") as f:
        for raw_card in ijson.items(f, "item"):
            try:
                card_row = _parse_card_row(raw_card)
                if card_row is None:
                    continue
                card_rows_buf.append(card_row)
                raw_cards_buf.append(raw_card)
            except Exception as exc:
                errors_count += 1
                log.warning(f"[PARSE] '{raw_card.get('name', '?')}': {exc}")
                continue

            if len(card_rows_buf) >= BATCH_SIZE:
                try:
                    c, p = _flush_batch(session, card_rows_buf, raw_cards_buf, today)
                    cards_imported += c
                    printings_imported += p
                except Exception as exc:
                    log.error(f"[BATCH] cards {cards_imported}–{cards_imported + BATCH_SIZE}: {exc}")
                    session.rollback()
                    errors_count += len(card_rows_buf)
                finally:
                    card_rows_buf = []
                    raw_cards_buf = []

                if cards_imported > 0 and cards_imported % 2_000 == 0:
                    log.info(
                        f"  -> {cards_imported:>6,} cartes  |  "
                        f"{printings_imported:>6,} impressions  |  "
                        f"{errors_count} erreurs"
                    )

    if card_rows_buf:
        try:
            c, p = _flush_batch(session, card_rows_buf, raw_cards_buf, today)
            cards_imported += c
            printings_imported += p
        except Exception as exc:
            log.error(f"[BATCH] dernier batch : {exc}")
            session.rollback()
            errors_count += len(card_rows_buf)

    return cards_imported, printings_imported, errors_count


# ══════════════════════════════════════════════════════════════════════════════
# 6. POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importe les cartes MTG depuis Scryfall bulk data vers PostgreSQL."
    )
    parser.add_argument("--force", action="store_true",
                        help="Retélécharge le fichier même s'il existe déjà.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sans insérer en base.")
    args = parser.parse_args()

    if not args.dry_run and not check_connection():
        log.error("Connexion PostgreSQL impossible. Vérifier DATABASE_URL dans .env.")
        sys.exit(1)

    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=300.0),
        headers=HTTP_HEADERS,
        follow_redirects=True,
    ) as client:

        log.info("Récupération des métadonnées Scryfall...")
        try:
            download_uri, filename, source_updated_at = fetch_bulk_metadata(client)
        except Exception as exc:
            log.error(f"Impossible de contacter Scryfall : {exc}")
            sys.exit(1)

        log.info(f"  Source   : {filename}")
        log.info(f"  Scryfall : mis à jour le {source_updated_at.strftime('%Y-%m-%d %H:%M UTC')}")

        dest = RAW_DIR / filename
        if dest.exists() and not args.force:
            log.info(f"Fichier déjà présent ({dest.stat().st_size / 1_048_576:.0f} Mo). Utilise --force pour retélécharger.")
        else:
            log.info(f"Téléchargement vers {dest} ...")
            download_bulk_file(client, download_uri, dest)

        if args.dry_run:
            log.info("[DRY-RUN] Comptage sans insertion...")
            count = 0
            with open(dest, "rb") as f:
                for _ in ijson.items(f, "item"):
                    count += 1
                    if count % 5_000 == 0:
                        log.info(f"  {count:,} objets parsés...")
            log.info(f"[DRY-RUN] Total : {count:,} objets.")
            return

        with SessionLocal() as session:
            run = ImportRun(
                source="scryfall",
                source_file=download_uri,
                source_updated_at=source_updated_at,
                status="running",
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            log.info(f"Import run #{run.id} démarré.")

            started_at = datetime.now(timezone.utc)
            try:
                log.info("Import des éditions...")
                n_sets = import_sets(client, session)
                log.info(f"  {n_sets} éditions importées/mises à jour.")

                log.info("Import des cartes (streaming, batches de 500)...")
                cards_n, printings_n, errors_n = import_cards(dest, session)

                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                run.status = "success"
                run.finished_at = datetime.now(timezone.utc)
                run.cards_imported = cards_n
                run.printings_imported = printings_n
                run.errors_count = errors_n
                session.commit()

                log.info("")
                log.info("=" * 47)
                log.info(f"  Cartes     : {cards_n:>10,}")
                log.info(f"  Impressions: {printings_n:>10,}")
                log.info(f"  Éditions   : {n_sets:>10,}")
                log.info(f"  Erreurs    : {errors_n:>10}")
                log.info(f"  Durée      : {elapsed:>9}s")
                log.info("=" * 47)

            except Exception as exc:
                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                log.error(f"Erreur fatale après {elapsed}s : {exc}", exc_info=True)
                run.status = "failed"
                run.finished_at = datetime.now(timezone.utc)
                run.error_message = str(exc)
                session.commit()
                sys.exit(1)


if __name__ == "__main__":
    main()
