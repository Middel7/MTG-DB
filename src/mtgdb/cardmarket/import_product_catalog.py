"""
Import du Product Catalog Cardmarket (Magic Singles).
Upsert dans cardmarket_products.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from mtgdb.cardmarket.parsers import CLES_RACINE_PRODUITS, iter_json_array, parse_product
from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
from mtgdb.db.models.cardmarket_product import CardmarketProduct

log = logging.getLogger("cardmarket.product_catalog")
BATCH_SIZE = 1000


def import_product_catalog(
    file_path: Path,
    session: Session,
    import_row: CardmarketImportFile,
) -> int:
    log.info(f"  Parsing {file_path.name} ({file_path.stat().st_size / 1_048_576:.1f} Mo)…")

    # Streaming : `json.load()` sur ce fichier pesait 203 Mo en mémoire, la plus
    # grosse part du pic mesuré du pipeline (350 Mo). Le compte exact n'est plus
    # connu d'avance — c'est le prix du streaming, et il est journalisé à la fin.
    products = iter_json_array(file_path, CLES_RACINE_PRODUITS)

    rows_imported = 0
    errors = 0
    product_batch: list[dict] = []

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
                # Un nom vide n'écrase jamais un nom connu.
                #
                # `parse_product()` retombe sur `default=""` quand Cardmarket ne
                # fournit ni `enName`, ni `en_name`, ni `name`. La colonne est
                # `NOT NULL` mais sans contrainte de non-vacuité : la chaîne vide
                # passe sans bruit, et l'upsert écrasait alors un nom déjà correct.
                # Un export appauvri d'une seule journée suffisait donc à vider le
                # catalogue, sans erreur et sans trace.
                #
                # Même raisonnement que `PRESERVE_IF_NULL` sur `cardmarket_id` :
                # une valeur absente de la source ne signifie pas « cette valeur a
                # disparu ». `NULLIF(…, '')` traite le vide comme l'absence qu'il
                # est réellement.
                #
                # Le sens inverse fonctionne toujours : un produit reconstitué par
                # `import_price_guide` avec `en_name = ''` reçoit bien son vrai nom
                # au premier passage du Product Catalog qui le contient.
                "en_name": func.coalesce(
                    func.nullif(stmt.excluded.en_name, ""),
                    CardmarketProduct.en_name,
                ),
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
        session.commit()
        rows_imported += len(product_batch)
        product_batch.clear()

    sans_nom = 0
    for raw in products:
        try:
            parsed = parse_product(raw)
            if parsed is None:
                errors += 1
                continue
            if not parsed["en_name"]:
                sans_nom += 1
            product_batch.append(parsed)
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

    # Un produit sans nom n'est pas une erreur — sa ligne est valide, son prix
    # exploitable, et le rapprochement avec Scryfall passe par `id_product`, pas
    # par le nom. Mais ce n'est pas normal non plus : le compter rend visible une
    # dégradation de la source qui, sinon, ne laisserait aucune trace.
    if sans_nom:
        log.warning(
            f"  {sans_nom:,} produit(s) sans nom dans l'export Cardmarket "
            f"({sans_nom / max(rows_imported, 1):.1%}). Leur nom déjà connu est "
            f"conservé ; ceux qui n'en ont jamais eu restent vides."
        )
    return rows_imported
