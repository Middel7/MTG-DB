"""Index fonctionnel lower(printed_name), et retrait du btree devenu inutile

DEUX GESTES, UNE SEULE RAISON
Les deux applications qui lisent cette base n'accèdent JAMAIS à printed_name
par sa valeur brute. Relevé dans leur code :

  ManaMind_AI  routers/collection.py:401   printed_name.ilike(f"{q}%")
  RELIC-Trade  deck_resolution.py:114      func.lower(printed_name) == …
  RELIC-Trade  deck_resolution.py:401      func.lower(printed_name).in_(…)

Le btree `ix_scryfall_card_printings_printed_name` ne peut servir aucune de ces
formes : ILIKE est insensible à la casse, et lower() est une fonction — un index
sur la colonne brute lui est inaccessible. Il coûtait 26 Mo et une écriture à
chaque import, pour zéro lecture.

Cette migration corrige les deux moitiés du problème :
  1. Un index fonctionnel sur lower(printed_name), qui sert enfin la résolution
     de decklist de RELIC-Trade (mesurée à 261 ms en Seq Scan, 645 Mo lus pour
     ramener 1 ligne).
  2. Le retrait du btree, dont l'inutilité est désormais établie par le code des
     consommateurs et non plus seulement par les compteurs.

L'autocomplétion de ManaMind_AI (`ILIKE 'q%'`) est déjà servie par l'index GIN
trigram de la migration précédente : les formes ancrées exploitent les trigrammes
de padding.

ORDRE DES OPÉRATIONS
Le nouvel index est créé AVANT que l'ancien ne soit supprimé. Les deux ne se
recouvrent pas, mais on ne laisse jamais la table sans filet le temps d'un build.

CONCURRENTLY, HORS TRANSACTION
Même contrainte que la migration précédente : table de 850 Mo lue par deux
applications, un CREATE INDEX bloquant gèlerait les écritures de l'import.
DROP INDEX CONCURRENTLY relève de la même règle et impose le bloc autocommit.

Détecter un index INVALID resté d'un run interrompu :

    SELECT c.relname
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
     WHERE NOT i.indisvalid;

Revision ID: 20260824_printed_name_lower
Revises: 20260824_printed_name_trgm
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_printed_name_lower"
down_revision: Union[str, Sequence[str], None] = "20260824_printed_name_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "scryfall_card_printings"
LOWER_INDEX = "ix_scryfall_card_printings_printed_name_lower"
BTREE_INDEX = "ix_scryfall_card_printings_printed_name"

MAINTENANCE_WORK_MEM = "512MB"


def _index_state(conn: sa.engine.Connection, name: str) -> str | None:
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
        {"name": name},
    ).scalar()
    if indisvalid is None:
        return None
    return "valid" if indisvalid else "invalid"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))

        # 1. Créer l'index fonctionnel — même garde que la migration trigram :
        #    on ne reconstruit que ce qui est absent ou cassé.
        state = _index_state(conn, LOWER_INDEX)
        if state == "valid":
            print(f"  {LOWER_INDEX} déjà présent et valide — création ignorée.")
        else:
            if state == "invalid":
                print(f"  {LOWER_INDEX} présent mais INVALID — reconstruction.")
                conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {LOWER_INDEX}"))
            conn.execute(
                sa.text(
                    f"""
                    CREATE INDEX CONCURRENTLY {LOWER_INDEX}
                        ON {TABLE_NAME} (lower(printed_name))
                    """
                )
            )

        # 2. Retirer le btree sur la colonne brute, une fois le remplaçant en place.
        conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {BTREE_INDEX}"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))

        # Rétablir le btree tel qu'il était (créé par `index=True` sur la colonne).
        if _index_state(conn, BTREE_INDEX) is None:
            conn.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {BTREE_INDEX} ON {TABLE_NAME} (printed_name)"
                )
            )
        conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {LOWER_INDEX}"))
