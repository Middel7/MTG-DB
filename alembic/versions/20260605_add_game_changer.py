"""add game_changer column to cards

Revision ID: a1b2c3d4e5f6
Revises: 392e971f7759
Create Date: 2026-06-05 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "392e971f7759"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cards",
        sa.Column("game_changer", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_cards_game_changer", "cards", ["game_changer"])


def downgrade() -> None:
    op.drop_index("ix_cards_game_changer", table_name="cards")
    op.drop_column("cards", "game_changer")
