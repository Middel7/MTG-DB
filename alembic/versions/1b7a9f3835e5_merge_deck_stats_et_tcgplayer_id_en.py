"""merge deck_stats et tcgplayer_id_en

Revision ID: 1b7a9f3835e5
Revises: 20260620_add_deck_stats_tables, 20260711_add_tcgplayer_id_en
Create Date: 2026-07-13 15:57:59.425088

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '1b7a9f3835e5'
down_revision: Union[str, Sequence[str], None] = ('20260620_add_deck_stats_tables', '20260711_add_tcgplayer_id_en')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
