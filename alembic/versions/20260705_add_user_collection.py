"""Ajoute la table user_collection

⚠️ TABLE SANS MODÈLE ET SANS PROPRIÉTAIRE DÉCLARÉ

Elle est créée ici mais n'a aucun modèle SQLAlchemy dans ce dépôt, et n'est citée
ni dans le README ni dans `docs/schema_base_de_donnees.txt`. Elle porte pourtant
des données réelles (4 577 lignes au 19/09/2026).

Elle survit à l'autogenerate par ACCIDENT, non par intention : le filtre
`include_object` d'`env.py` ignore toute table présente en base et absente des
modèles, en supposant qu'elle appartient à un autre projet. `user_collection`
bénéficie de cette clause sans en relever vraiment, puisque c'est MTG-DB qui l'a
créée.

Deux issues, à trancher avec ManaMind_AI :

  - MTG-DB en est propriétaire → lui donner un modèle, comme aux autres tables
    de ce dépôt, et la documenter ;
  - ManaMind_AI en est propriétaire → l'ajouter à `FOREIGN_TABLES` et déplacer
    cette migration chez lui.

Tant que ce n'est pas tranché, personne ne sait qui a le droit de la faire
évoluer — et une colonne ajoutée d'un côté surprendra l'autre.

Revision ID: 20260705_add_user_collection
Revises: 20260620_add_scryfall_card_tags
Create Date: 2026-07-05
"""
import sqlalchemy as sa
from alembic import op

revision = "20260705_add_user_collection"
down_revision = "20260620_add_scryfall_card_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_collection",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("raw_line", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_collection_card_name", "user_collection", ["card_name"])


def downgrade() -> None:
    op.drop_index("ix_user_collection_card_name", table_name="user_collection")
    op.drop_table("user_collection")
