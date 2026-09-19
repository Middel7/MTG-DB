"""Retire ix_cm_price_guide_id_product, couvert par le composite

POURQUOI
`cardmarket_price_guide_entries` porte deux index qui commencent par la même
colonne :

    ix_cm_price_guide_id_product        (id_product)                60 Mo
    ix_cm_price_guide_product_captured  (id_product, captured_at)  229 Mo

Le second couvre le premier : PostgreSQL sait utiliser le préfixe gauche d'un
index composite. Le simple ne subsistait que parce qu'il est plus petit, donc
parfois préféré par le planner — 441 493 parcours contre 5 447 031 pour le
composite.

MESURE, ET NON SUPPOSITION
La suppression a été simulée dans une transaction annulée sur la base locale
(6 320 513 lignes), en comparant le même `EXPLAIN ANALYZE` avant et après :

    SELECT * FROM cardmarket_price_guide_entries WHERE id_product = 763642

    avec le simple    Index Scan using ix_cm_price_guide_id_product
                      Execution Time: 0.660 ms
    sans le simple    Index Scan using ix_cm_price_guide_product_captured
                      Execution Time: 0.098 ms

Non seulement le composite prend le relais, mais il est plus rapide : ses pages
sont déjà en cache, puisque c'est lui que servent toutes les autres requêtes —
dont la vue `v_cardmarket_latest_prices_by_printing`.

LA CLÉ ÉTRANGÈRE RESTE COUVERTE
`id_product` référence `cardmarket_products` avec `ON DELETE SET NULL`. Sans
index, supprimer un produit imposerait un parcours séquentiel des 6,3 millions de
lignes. Le composite remplit ce rôle par son préfixe gauche — c'est ce que montre
la mesure ci-dessus, où le planner l'utilise pour une recherche sur `id_product`
seul.

GAIN
60 Mo, et une écriture d'index en moins par insertion — soit ~126 000 par jour,
sur l'instance de production qui est déjà le goulot du pipeline.

CONCURRENTLY, HORS TRANSACTION
La table est lue en continu par RELIC-Trade. `DROP INDEX CONCURRENTLY` attend la
fin des transactions en cours plutôt que de les bloquer, et impose donc le bloc
autocommit.

Revision ID: 20260919_drop_idx_redondant
Revises: 20260919_default_prix_date
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_drop_idx_redondant"
down_revision: Union[str, Sequence[str], None] = "20260919_default_prix_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "cardmarket_price_guide_entries"
INDEX_REDONDANT = "ix_cm_price_guide_id_product"
INDEX_COMPOSITE = "ix_cm_price_guide_product_captured"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()

        # Ne jamais retirer le simple si le composite manque : ce serait laisser
        # la clé étrangère sans index du tout. Le cas se produit sur une base où
        # la migration initiale aurait été jouée partiellement.
        composite = conn.execute(
            sa.text("SELECT count(*) FROM pg_indexes WHERE indexname = :nom"),
            {"nom": INDEX_COMPOSITE},
        ).scalar()
        if not composite:
            raise RuntimeError(
                f"{INDEX_COMPOSITE} est absent : retirer {INDEX_REDONDANT} laisserait "
                f"la clé étrangère id_product sans index, et une suppression de produit "
                f"parcourrait les 6,3 millions de lignes. Migration interrompue."
            )

        conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_REDONDANT}"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text("SET maintenance_work_mem = '512MB'"))
        existe = conn.execute(
            sa.text("SELECT count(*) FROM pg_indexes WHERE indexname = :nom"),
            {"nom": INDEX_REDONDANT},
        ).scalar()
        if not existe:
            conn.execute(sa.text(
                f"CREATE INDEX CONCURRENTLY {INDEX_REDONDANT} ON {TABLE_NAME} (id_product)"))
