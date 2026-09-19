"""Ajoute scryfall_cards.tagger_checked_at

POURQUOI
`import_tagger_tags.py` ne traite, par défaut, que les cartes SANS tag. Une carte
que Tagger connaît mais qu'il n'a taguée avec rien reste donc éternellement
« sans tag » : elle est réinterrogée à chaque passage hebdomadaire, indéfiniment,
à raison d'une requête HTTP et de 0,2 s de pause chacune.

Le comportement était connu et assumé (`render.yaml` le mentionne), mais son coût
est permanent et croît avec le catalogue.

CE QUE LA COLONNE CHANGE
Elle enregistre la date du dernier passage de Tagger sur la carte, qu'il ait
produit des tags ou non. La sélection peut alors écarter ce qui a été vérifié
récemment, au lieu de se fonder sur la seule présence de tags.

Une carte sans tag reste réinterrogée — Tagger enrichit son catalogue en
permanence — mais après un délai, et non à chaque run.

FORME
`ADD COLUMN` d'une colonne NULLable : opération de métadonnée, instantanée, sans
réécriture des 38 907 lignes. L'index partiel ne couvre que les cartes déjà
vérifiées, les seules que la requête écarte.

Revision ID: 20260919_tagger_checked_at
Revises: 20260919_drop_idx_redondant
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_tagger_checked_at"
down_revision: Union[str, Sequence[str], None] = "20260919_drop_idx_redondant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "scryfall_cards"
COLUMN_NAME = "tagger_checked_at"
INDEX_NAME = "ix_scryfall_cards_tagger_checked_at"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {COLUMN_NAME} "
        f"TIMESTAMP WITH TIME ZONE"
    )
    # Index PARTIEL : la requête de sélection ne s'intéresse qu'aux cartes déjà
    # vérifiées, pour les écarter. Les NULL — l'immense majorité au départ — n'ont
    # rien à faire dans l'index.
    with op.get_context().autocommit_block():
        op.get_bind().execute(sa.text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            f"ON {TABLE_NAME} ({COLUMN_NAME}) WHERE {COLUMN_NAME} IS NOT NULL"))


def downgrade() -> None:
    # La colonne part avec son index. Aucune donnée métier n'est perdue : elle ne
    # contient qu'un horodatage de contrôle, reconstitué au prochain passage.
    with op.get_context().autocommit_block():
        op.get_bind().execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS {COLUMN_NAME}")
