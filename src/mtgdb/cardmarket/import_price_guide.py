"""
Import du Price Guide Cardmarket.
Chaque import crée un snapshot historisé dans cardmarket_price_guide_entries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from mtgdb.cardmarket.parsers import (
    CLES_RACINE_PRICE_GUIDE,
    iter_json_array,
    parse_price_guide_entry,
)
from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
from mtgdb.db.models.cardmarket_price_guide_entry import CardmarketPriceGuideEntry
from mtgdb.db.models.cardmarket_product import CardmarketProduct

log = logging.getLogger("cardmarket.price_guide")
BATCH_SIZE = 2000


def purge_old_captures(session: Session, keep: int) -> int:
    """
    Ne conserve que les `keep` relevés les plus récents **par produit**. Retourne
    le nombre de lignes supprimées.

    ⚠️ RÉSERVÉ AU POSTE LOCAL. En production, la rétention de cette table est un
    arbitrage métier de RELIC-Trade — c'est son API qui la lit, et une purge
    pilotée depuis le dépôt qui écrit serait invisible depuis celui qui lit.
    RELIC-Trade la traite par son propre `apps/api/scripts/purge_price_history.py`.
    Ne pas activer `--keep-captures` sur un run visant la production.

    Une capture pèse ~62 Mo (~126 000 lignes, une par produit) ; en quotidien, la
    table croît de ~23 Go par an. En local, l'historique sert à comparer des
    relevés — d'où le choix, ici, d'un réglage plutôt que d'une suppression
    systématique.

    POURQUOI PAR PRODUIT ET NON PAR DATE
    Purger globalement — « supprimer tout ce qui n'est pas dans les N derniers
    `captured_at` » — efface le prix d'un produit absent de ces N derniers
    imports. Cardmarket publie un catalogue complet à chaque fois, mais un
    produit retiré du catalogue verrait son dernier prix connu disparaître, sans
    moyen de le reconstituer : Cardmarket ne republie pas ses relevés passés.
    On garde donc les `keep` derniers relevés DE CHAQUE produit.

    `keep=0` désactive la purge — c'est le défaut, pour ne rien supprimer sans
    demande explicite.

    ⚠️ Suppression DÉFINITIVE.
    """
    if keep <= 0:
        return 0

    total_captures = session.scalar(
        select(func.count(func.distinct(CardmarketPriceGuideEntry.captured_at)))
    ) or 0
    if total_captures <= keep:
        log.info(f"  Purge des captures : {total_captures} capture(s) ≤ {keep} — rien à faire.")
        return 0

    # row_number() par produit : chaque id_product conserve ses `keep` relevés
    # les plus récents, quelle que soit leur date. Un produit qui n'apparaît que
    # dans un import ancien garde donc sa ligne.
    ranked = (
        select(
            CardmarketPriceGuideEntry.id,
            func.row_number()
            .over(
                partition_by=CardmarketPriceGuideEntry.id_product,
                order_by=CardmarketPriceGuideEntry.captured_at.desc(),
            )
            .label("rn"),
        )
        .subquery()
    )
    doomed = select(ranked.c.id).where(ranked.c.rn > keep)

    deleted = session.execute(
        CardmarketPriceGuideEntry.__table__
        .delete()
        .where(CardmarketPriceGuideEntry.id.in_(doomed))
    ).rowcount
    session.commit()

    log.info(
        f"  Purge des captures : {deleted:,} ligne(s) supprimée(s) — "
        f"{keep} relevé(s) conservé(s) par produit."
    )
    return deleted


def import_price_guide(
    file_path: Path,
    session: Session,
    import_row: CardmarketImportFile,
) -> int:
    log.info(f"  Parsing {file_path.name} ({file_path.stat().st_size / 1_048_576:.1f} Mo)…")

    # Streaming, pour la même raison que le Product Catalog : le fichier entier
    # ne tient plus en mémoire par principe, quelle que soit sa croissance.
    entries = iter_json_array(file_path, CLES_RACINE_PRICE_GUIDE)

    captured_at = datetime.now(timezone.utc)
    rows_imported = 0
    errors = 0
    produits_crees = 0
    batch: list[dict] = []

    def flush() -> None:
        nonlocal rows_imported, produits_crees
        if not batch:
            return

        # Les produits absents de `cardmarket_products` sont CRÉÉS, pas oubliés.
        #
        # Le code d'origine mettait leur `id_product` à NULL pour esquiver la
        # violation de clé étrangère, et insérait la ligne quand même. Trois
        # dégâts, mesurés le 19/09 sur 252 414 lignes (4 % de la table) :
        #
        #   - le prix devient inexploitable en SQL : plus rien ne le rattache à
        #     un produit, l'information ne survit que dans `raw_json` ;
        #   - `ON CONFLICT (import_file_id, id_product)` cesse d'agir, puisqu'en
        #     SQL un NULL n'entre jamais en conflit avec un autre NULL : rejouer
        #     le même fichier dupliquerait ces lignes ;
        #   - le compteur ment, `rows_imported` comptant les lignes proposées.
        #
        # Le cas n'est pas rare : le Product Catalog et le Price Guide sont
        # téléchargés séparément, et le catalogue est souvent `skipped_not_modified`
        # alors que le Price Guide, lui, contient déjà les produits du jour.
        #
        # La ligne créée ici est volontairement minimale — `en_name` vide, un
        # `raw_json` qui dit d'où elle vient. Le prochain passage du Product
        # Catalog l'enrichira par son upsert habituel, sans rien de particulier
        # à prévoir.
        ids_du_lot = {r["id_product"] for r in batch if r.get("id_product")}
        connus = {
            ligne[0] for ligne in session.execute(
                select(CardmarketProduct.id_product)
                .where(CardmarketProduct.id_product.in_(ids_du_lot))
            )
        }
        manquants = ids_du_lot - connus
        if manquants:
            session.execute(
                pg_insert(CardmarketProduct)
                .values([
                    {
                        "id_product": pid,
                        "en_name": "",
                        "raw_json": {
                            "idProduct": pid,
                            "_source": "price_guide",
                            "_note": "produit vu dans le Price Guide avant le Product Catalog",
                        },
                    }
                    for pid in sorted(manquants)
                ])
                .on_conflict_do_nothing(index_elements=["id_product"])
            )
            produits_crees += len(manquants)

        stmt = pg_insert(CardmarketPriceGuideEntry).values(batch)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["import_file_id", "id_product"]
        )
        resultat = session.execute(stmt)
        session.commit()
        # `rowcount` et non `len(batch)` : avec DO NOTHING, les deux diffèrent dès
        # qu'une ligne est écartée, et c'est précisément ce qu'on veut savoir.
        rows_imported += resultat.rowcount if resultat.rowcount >= 0 else len(batch)
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
    if produits_crees:
        log.info(
            f"  {produits_crees:,} produit(s) inconnu(s) créé(s) au passage — ils "
            f"seront renseignés au prochain import du Product Catalog."
        )
    return rows_imported
