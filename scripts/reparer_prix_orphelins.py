#!/usr/bin/env python3
"""
Rattache les prix Cardmarket dont l'`id_product` a été effacé à l'import.

POURQUOI CE SCRIPT EXISTE
Jusqu'au 19/09/2026, `import_price_guide()` mettait `id_product` à NULL quand le
produit était absent de `cardmarket_products`, pour esquiver une violation de clé
étrangère — et insérait la ligne quand même. 252 414 lignes (4 % de la table) sont
dans cet état : leur prix existe, mais plus rien ne le rattache à un produit.

L'import ne produit plus de telles lignes. Ce script répare celles qui restent.

CE QU'IL FAUT SAVOIR
L'information n'est pas perdue : `raw_json` conserve l'`idProduct` d'origine. Le
rattachement est donc une simple relecture, sans rien inventer.

Deux cas se présentent :

  - le produit existe aujourd'hui dans `cardmarket_products` (il est arrivé au
    passage suivant du Product Catalog) : la ligne est rattachée ;
  - il n'existe toujours pas : le produit a probablement été retiré du catalogue
    Cardmarket. Le script le CRÉE, comme le fait désormais l'import, plutôt que
    de laisser le prix inexploitable.

POURQUOI PAS UNE MIGRATION ALEMBIC
C'est une réparation de données historiques, pas une évolution de schéma. Elle
doit être lancée délibérément, avec un `--dry-run` préalable et sous surveillance
— pas au milieu d'un `alembic upgrade head` joué en production un soir de
déploiement.

Usage :
    python scripts/reparer_prix_orphelins.py --dry-run   # compte, n'écrit rien
    python scripts/reparer_prix_orphelins.py             # répare
    python scripts/reparer_prix_orphelins.py --lot 20000 # taille des lots
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("reparer_prix_orphelins")

# Lots plutôt qu'un UPDATE unique : la table fait 3,4 Go et l'instance de
# production tourne sur 0,1 vCPU. Une transaction de 252 414 lignes y tiendrait
# un verrou et gonflerait le WAL sans nécessité — rien n'exige que la réparation
# soit atomique, chaque ligne étant indépendante.
LOT_PAR_DEFAUT = 10_000


def compter(session) -> tuple[int, int, int]:
    """(orphelines, dont le produit existe, dont le produit est inconnu)."""
    total = session.execute(text("""
        SELECT count(*) FROM cardmarket_price_guide_entries WHERE id_product IS NULL
    """)).scalar_one()
    rattachables = session.execute(text("""
        SELECT count(*)
          FROM cardmarket_price_guide_entries e
          JOIN cardmarket_products p
            ON p.id_product = (e.raw_json ->> 'idProduct')::bigint
         WHERE e.id_product IS NULL
           AND jsonb_typeof(e.raw_json -> 'idProduct') = 'number'
    """)).scalar_one()
    return total, rattachables, total - rattachables


def creer_produits_manquants(session, dry_run: bool) -> int:
    """Crée les produits que le Price Guide connaît et que le catalogue ignore."""
    if dry_run:
        return session.execute(text("""
            SELECT count(DISTINCT (raw_json ->> 'idProduct')::bigint)
              FROM cardmarket_price_guide_entries e
             WHERE e.id_product IS NULL
               AND jsonb_typeof(e.raw_json -> 'idProduct') = 'number'
               AND NOT EXISTS (SELECT 1 FROM cardmarket_products p
                                WHERE p.id_product = (e.raw_json ->> 'idProduct')::bigint)
        """)).scalar_one()

    resultat = session.execute(text("""
        INSERT INTO cardmarket_products (id_product, en_name, raw_json)
        SELECT DISTINCT (e.raw_json ->> 'idProduct')::bigint,
               '',
               jsonb_build_object(
                   'idProduct', (e.raw_json ->> 'idProduct')::bigint,
                   '_source', 'price_guide',
                   '_note', 'produit reconstitue depuis un prix orphelin')
          FROM cardmarket_price_guide_entries e
         WHERE e.id_product IS NULL
           AND jsonb_typeof(e.raw_json -> 'idProduct') = 'number'
        ON CONFLICT (id_product) DO NOTHING
    """))
    session.commit()
    return resultat.rowcount


def rattacher(session, taille_lot: int) -> int:
    """Rattache les prix orphelins, par lots. Retourne le total réparé."""
    total = 0
    while True:
        resultat = session.execute(text("""
            UPDATE cardmarket_price_guide_entries
               SET id_product = (raw_json ->> 'idProduct')::bigint
             WHERE id IN (
                 SELECT e.id
                   FROM cardmarket_price_guide_entries e
                  WHERE e.id_product IS NULL
                    AND jsonb_typeof(e.raw_json -> 'idProduct') = 'number'
                    AND EXISTS (SELECT 1 FROM cardmarket_products p
                                 WHERE p.id_product = (e.raw_json ->> 'idProduct')::bigint)
                  LIMIT :lot
             )
        """), {"lot": taille_lot})
        session.commit()
        if not resultat.rowcount:
            break
        total += resultat.rowcount
        log.info(f"  {total:,} ligne(s) rattachée(s)…")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compte ce qui serait réparé, sans rien écrire.")
    parser.add_argument("--lot", type=int, default=LOT_PAR_DEFAUT, metavar="N",
                        help=f"Lignes par transaction (défaut : {LOT_PAR_DEFAUT}).")
    args = parser.parse_args()

    if not check_connection():
        log.error("Connexion PostgreSQL impossible.")
        sys.exit(1)

    with SessionLocal() as session:
        orphelines, rattachables, sans_produit = compter(session)

        log.info("=" * 60)
        log.info(f"  Prix orphelins (id_product NULL) : {orphelines:>10,}")
        log.info(f"    dont le produit existe déjà    : {rattachables:>10,}")
        log.info(f"    dont le produit est inconnu    : {sans_produit:>10,}")
        log.info("=" * 60)

        if not orphelines:
            log.info("Rien à réparer.")
            return

        crees = creer_produits_manquants(session, args.dry_run)
        if args.dry_run:
            log.info(f"[DRY-RUN] {crees:,} produit(s) seraient créé(s).")
            log.info(f"[DRY-RUN] {orphelines:,} ligne(s) seraient rattachée(s).")
            log.info("[DRY-RUN] Aucune écriture effectuée.")
            return

        log.info(f"  {crees:,} produit(s) créé(s) depuis les prix orphelins.")
        repares = rattacher(session, args.lot)

        restants, _, _ = compter(session)
        log.info("")
        log.info(f"  Réparées  : {repares:,}")
        log.info(f"  Restantes : {restants:,}")
        if restants:
            log.warning(
                f"  {restants:,} ligne(s) restent orphelines : leur raw_json ne "
                f"contient pas d'idProduct numérique exploitable."
            )


if __name__ == "__main__":
    main()
