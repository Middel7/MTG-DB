"""
Import du Product Catalog Cardmarket (Magic Singles).
Upsert dans cardmarket_products + cardmarket_product_localizations.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from mtgdb.cardmarket.parsers import extract_products_list, parse_localizations, parse_product
from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
from mtgdb.db.models.cardmarket_product import CardmarketProduct
from mtgdb.db.models.cardmarket_product_localization import CardmarketProductLocalization

log = logging.getLogger("cardmarket.product_catalog")
BATCH_SIZE = 1000


def import_product_catalog(
    file_path: Path,
    session: Session,
    import_row: CardmarketImportFile,
) -> int:
    log.info(f"  Parsing {file_path.name} ({file_path.stat().st_size / 1_048_576:.1f} Mo)…")

    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    products = extract_products_list(data)
    log.info(f"  {len(products):,} produits trouvés.")

    rows_imported = 0
    errors = 0
    product_batch: list[dict] = []
    loc_batch: list[dict] = []

    def flush() -> None:
        nonlocal rows_imported
        if not product_batch:
            return

        stmt = pg_insert(CardmarketProduct).values(product_batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id_product"],
            set_={
                "id_metaproduct":  stmt.excluded.id_metaproduct,
                "count_reprints":  stmt.excluded.count_reprints,
                "en_name":         stmt.excluded.en_name,
                "website":         stmt.excluded.website,
                "image":           stmt.excluded.image,
                "game_name":       stmt.excluded.game_name,
                "category_name":   stmt.excluded.category_name,
                "number":          stmt.excluded.number,
                "rarity":          stmt.excluded.rarity,
                "expansion_name":  stmt.excluded.expansion_name,
                "raw_json":        stmt.excluded.raw_json,
                "last_seen_at":    func.now(),
                "updated_at":      func.now(),
            },
        )
        session.execute(stmt)

        if loc_batch:
            loc_stmt = pg_insert(CardmarketProductLocalization).values(loc_batch)
            loc_stmt = loc_stmt.on_conflict_do_update(
                index_elements=["id_product", "id_language"],
                set_={
                    "language_name": loc_stmt.excluded.language_name,
                    "product_name":  loc_stmt.excluded.product_name,
                },
            )
            session.execute(loc_stmt)

        session.commit()
        rows_imported += len(product_batch)
        product_batch.clear()
        loc_batch.clear()

    for raw in products:
        try:
            parsed = parse_product(raw)
            if parsed is None:
                errors += 1
                continue
            product_batch.append(parsed)
            loc_batch.extend(parse_localizations(raw, parsed["id_product"]))
        except Exception as exc:
            errors += 1
            log.warning(f"  [PARSE] produit ignoré : {exc}")
            continue

        if len(product_batch) >= BATCH_SIZE:
            flush()

    flush()

    import_row.rows_imported = rows_imported
    import_row.errors_count = errors
    import_row.status = "success"
    import_row.finished_at = datetime.now(timezone.utc)
    session.commit()

    log.info(f"  Produits importés : {rows_imported:,} | Erreurs : {errors}")
    return rows_imported
