"""
Import du Price Guide Cardmarket.
Chaque import crée un snapshot historisé dans cardmarket_price_guide_entries.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from mtgdb.cardmarket.parsers import extract_price_guide_list, parse_price_guide_entry
from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
from mtgdb.db.models.cardmarket_price_guide_entry import CardmarketPriceGuideEntry

log = logging.getLogger("cardmarket.price_guide")
BATCH_SIZE = 2000


def import_price_guide(
    file_path: Path,
    session: Session,
    import_row: CardmarketImportFile,
) -> int:
    log.info(f"  Parsing {file_path.name} ({file_path.stat().st_size / 1_048_576:.1f} Mo)…")

    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    entries = extract_price_guide_list(data)
    log.info(f"  {len(entries):,} entrées Price Guide trouvées.")

    captured_at = datetime.now(timezone.utc)
    rows_imported = 0
    errors = 0
    batch: list[dict] = []

    def flush() -> None:
        nonlocal rows_imported
        if not batch:
            return
        # Filtrer les id_product qui n'existent pas dans cardmarket_products
        from sqlalchemy import select as sa_select
        from mtgdb.db.models.cardmarket_product import CardmarketProduct
        known_ids = {
            row[0] for row in session.execute(
                sa_select(CardmarketProduct.id_product).where(
                    CardmarketProduct.id_product.in_([r["id_product"] for r in batch if r.get("id_product")])
                )
            )
        }
        valid = []
        for r in batch:
            pid = r.get("id_product")
            if pid is None or pid in known_ids:
                valid.append(r)
            else:
                r["id_product"] = None
                valid.append(r)

        stmt = pg_insert(CardmarketPriceGuideEntry).values(valid)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["import_file_id", "id_product"]
        )
        session.execute(stmt)
        session.commit()
        rows_imported += len(valid)
        batch.clear()

    for raw in entries:
        try:
            parsed = parse_price_guide_entry(raw)
            if parsed is None:
                errors += 1
                continue
            parsed["import_file_id"] = import_row.id
            parsed["captured_at"] = captured_at
            batch.append(parsed)
        except Exception as exc:
            errors += 1
            log.warning(f"  [PARSE] entrée ignorée : {exc}")
            continue

        if len(batch) >= BATCH_SIZE:
            flush()

    flush()

    import_row.rows_imported = rows_imported
    import_row.errors_count = errors
    import_row.status = "success"
    import_row.finished_at = datetime.now(timezone.utc)
    session.commit()

    log.info(f"  Entrées importées : {rows_imported:,} | Erreurs : {errors}")
    return rows_imported
