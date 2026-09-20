"""Cree scryfall_card_parts : les cartes liees (jetons, emblemes, fusions)

POURQUOI
Rien en base ne disait quels jetons une carte met en jeu. La question « de quels
jetons ai-je besoin pour jouer ce deck ? » ne pouvait recevoir qu'une reponse
devinee, en cherchant « create ... token » dans le texte d'oracle — formulation
qui ne nomme ni la bonne variante (trois jetons Soldier blancs 1/1 coexistent),
ni les jetons crees indirectement.

Scryfall publie la reponse exacte dans le champ `all_parts` du bulk, ignore
jusqu'ici par l'import. Cette table l'accueille.

FORME
`part_scryfall_id` pointe une IMPRESSION (`scryfall_card_printings.scryfall_id`),
parce que c'est ce que `all_parts` reference. Aucune cle etrangere vers cette
colonne : l'ordre d'ecriture du pipeline ne garantit pas que l'impression citee
soit deja inseree au moment ou la ligne de liaison l'est, et une contrainte
differable ferait echouer un lot entier pour un jeton manquant.

La table part vide ; le prochain import complet la remplit.

Revision ID: 20260920_card_parts
Revises: 20260919_tagger_checked_at
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_card_parts"
down_revision: Union[str, Sequence[str], None] = "20260919_tagger_checked_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "scryfall_card_parts"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("component", sa.String(length=20), nullable=False),
        sa.Column("part_scryfall_id", sa.String(length=36), nullable=False),
        sa.Column("part_name", sa.String(length=255), nullable=True),
        sa.Column("part_type_line", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["card_id"], ["scryfall_cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "card_id", "part_scryfall_id", "component", name="uq_card_parts_carte_part"
        ),
        if_not_exists=True,
    )
    # Les deux sens de lecture sont utilises : « les jetons de cette carte »
    # (card_id) au rendu d'un deck, « quelles cartes produisent ce jeton »
    # (part_scryfall_id) pour remonter du jeton a ses sources.
    op.create_index(
        "ix_scryfall_card_parts_card_id", TABLE_NAME, ["card_id"], if_not_exists=True
    )
    op.create_index(
        "ix_scryfall_card_parts_part_scryfall_id",
        TABLE_NAME,
        ["part_scryfall_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table(TABLE_NAME, if_exists=True)
