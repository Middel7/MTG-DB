"""Stocke idExpansion du Product Catalog Cardmarket en colonne

POURQUOI
Le Product Catalog de Cardmarket ne fournit que sept champs par produit :
name, dateAdded, idProduct, idCategory, idMetacard, idExpansion, categoryName.

Les colonnes `expansion_name`, `number`, `rarity`, `website` et `image` existent
dans notre modèle mais restent VIDES pour les 121 190 produits — non par défaut
de parsing, mais parce que la source ne les contient pas. Le seul discriminant
disponible est donc `idExpansion`, qui n'était pas extrait.

Sans lui, deux produits de même nom sont indiscernables en SQL. Le cas qui a
motivé cette migration :

    Jace Reawakened, produit 763642 : idExpansion 5662  → 1,70 € / foil 2,84 €
    Jace Reawakened, produit 811768 : idExpansion 1249  → 21,23 € / foil 21,23 €

Scryfall associe l'impression `otj #271` au second — un produit de l'expansion
promo 1249, dont le prix est correct POUR CE PRODUIT mais 20× trop élevé pour la
carte d'édition normale. Le diagnostic a demandé de fouiller le `raw_json` faute
de colonne.

CE QUE CETTE COLONNE N'EST PAS
Un garde-fou. Il est tentant d'en déduire une règle « l'expansion Cardmarket doit
correspondre au set Scryfall » ; c'est faux, et mesuré comme tel :

  - un set Scryfall se répartit légitimement sur PLUSIEURS expansions Cardmarket
    (`otj` en compte trois : 5647 avec 284 impressions, 5662 avec 81, 5669 avec 6,
    à cause de Breaking News et Big Score) ;
  - signaler les impressions dont l'expansion est minoritaire (<1 %) donne
    1 131 signalements pour 10 vrais problèmes — 886 faux positifs, et 311 des
    321 prix réellement aberrants passeraient à travers.

Le contrôle qui fonctionne porte sur le PRIX, pas sur l'expansion :
`foil_low > 10 × foil_trend` isole les 321 cas sans faux positif. Il appartient à
RELIC-Trade, au point où la décision de rachat se prend.

Cette colonne sert donc au DIAGNOSTIC et à l'audit des liaisons, pas au filtrage.

FORME DE LA MIGRATION
`ADD COLUMN` d'une colonne NULLable est une opération de métadonnée : instantanée,
sans réécriture de table. Le backfill lit `raw_json`, déjà présent, donc aucune
donnée n'est à retélécharger. L'index est créé CONCURRENTLY : la table est lue en
continu par RELIC-Trade.

Revision ID: 20260826_cardmarket_id_expansion
Revises: 20260824_printed_name_lower
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_cardmarket_id_expansion"
down_revision: Union[str, Sequence[str], None] = "20260824_printed_name_lower"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "cardmarket_products"
COLUMN_NAME = "id_expansion"
INDEX_NAME = "ix_cardmarket_products_id_expansion"


def _index_state(conn: sa.engine.Connection) -> str | None:
    """'valid', 'invalid', ou None si l'index n'existe pas."""
    indisvalid = conn.execute(
        sa.text(
            """
            SELECT i.indisvalid
              FROM pg_class c
              JOIN pg_index i ON i.indexrelid = c.oid
             WHERE c.relname = :name
            """
        ),
        {"name": INDEX_NAME},
    ).scalar()
    if indisvalid is None:
        return None
    return "valid" if indisvalid else "invalid"


def upgrade() -> None:
    conn = op.get_bind()

    # IF NOT EXISTS : rend la migration rejouable après une interruption entre
    # l'ajout de colonne et la création de l'index (celle-ci étant hors
    # transaction, elle n'est pas annulée avec le reste).
    op.execute(
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {COLUMN_NAME} BIGINT"
    )

    # Backfill depuis raw_json. `jsonb_typeof` écarte les valeurs non numériques
    # plutôt que de faire échouer le cast sur une ligne isolée.
    result = conn.execute(
        sa.text(
            f"""
            UPDATE {TABLE_NAME}
               SET {COLUMN_NAME} = (raw_json ->> 'idExpansion')::bigint
             WHERE {COLUMN_NAME} IS NULL
               AND jsonb_typeof(raw_json -> 'idExpansion') = 'number'
            """
        )
    )
    print(f"  {COLUMN_NAME} : {result.rowcount:,} produit(s) renseigné(s) depuis raw_json.")

    with op.get_context().autocommit_block():
        c = op.get_bind()
        state = _index_state(c)
        if state == "valid":
            print(f"  {INDEX_NAME} déjà présent et valide — rien à faire.")
            return
        if state == "invalid":
            print(f"  {INDEX_NAME} présent mais INVALID — reconstruction.")
            c.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
        c.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON {TABLE_NAME} ({COLUMN_NAME})"
            )
        )


def downgrade() -> None:
    # La colonne part avec son index : inutile de le supprimer séparément.
    # Aucune donnée n'est perdue — `raw_json` conserve `idExpansion`, le backfill
    # est rejouable à l'identique.
    with op.get_context().autocommit_block():
        op.get_bind().execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS {COLUMN_NAME}")
