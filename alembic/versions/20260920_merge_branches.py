"""Fusionne les deux branches de migration issues du 20/09

POURQUOI
Deux migrations ont ete creees le meme jour avec le meme parent
(`20260919_tagger_checked_at`) : `20260920_printing_image_status` et
`20260920_card_parts`. L'arbre Alembic avait donc DEUX tetes.

Consequence concrete : `alembic upgrade head` echoue avec
« Multiple head revisions are present ». Il faut `upgrade heads` au pluriel, ce
que personne ne pense a taper — et la procedure de migration de la production
documentee dans docs/deploiement.md utilise bien `head` au singulier. La
divergence serait donc apparue au pire moment, au milieu d'une mise a jour de
schema en production.

Cette revision ne modifie rien : elle recoud simplement les deux branches.

Revision ID: 20260920_merge_branches
Revises: 20260920_printing_image_status, 20260920_card_parts
Create Date: 2026-09-20
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20260920_merge_branches"
down_revision: Union[str, Sequence[str], None] = (
    "20260920_printing_image_status",
    "20260920_card_parts",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rien a appliquer : une fusion ne porte aucun changement de schema."""


def downgrade() -> None:
    """Rien a annuler : redescendre recree simplement les deux branches."""
