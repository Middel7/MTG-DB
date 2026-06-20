"""Ajoute les tables deck_stat_global et deck_stat_commander

Revision ID: 20260620_add_deck_stats_tables
Revises: 20260620_add_scryfall_card_tags
Create Date: 2026-06-20
"""
import sqlalchemy as sa
from alembic import op

revision = "20260620_add_deck_stats_tables"
down_revision = "20260620_add_scryfall_card_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deck_stat_global",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_name", sa.Text(), nullable=False),
        sa.Column("decks_count", sa.BigInteger(), nullable=False),
        sa.Column("total_decks", sa.BigInteger(), nullable=False),
        sa.Column("global_frequency", sa.Float(), nullable=False),
        sa.Column("commanders_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idf", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_name", name="uq_deck_stat_global_card_name"),
    )
    op.create_index("ix_deck_stat_global_card_name", "deck_stat_global", ["card_name"])

    op.create_table(
        "deck_stat_commander",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commander", sa.String(255), nullable=False),
        sa.Column("card_name", sa.Text(), nullable=False),
        sa.Column("decks_with_card", sa.Integer(), nullable=False),
        sa.Column("total_decks", sa.Integer(), nullable=False),
        sa.Column("inclusion_rate", sa.Float(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commander", "card_name", name="uq_deck_stat_commander_card"),
    )
    op.create_index("ix_deck_stat_commander_commander", "deck_stat_commander", ["commander"])
    op.create_index("ix_deck_stat_commander_card_name", "deck_stat_commander", ["card_name"])


def downgrade() -> None:
    op.drop_index("ix_deck_stat_commander_card_name", table_name="deck_stat_commander")
    op.drop_index("ix_deck_stat_commander_commander", table_name="deck_stat_commander")
    op.drop_table("deck_stat_commander")
    op.drop_index("ix_deck_stat_global_card_name", table_name="deck_stat_global")
    op.drop_table("deck_stat_global")
