"""Supprime les tables inutilisees card_pricing_rules et cardmarket_product_localizations

Revision ID: 20260609_drop_unused_tables
Revises: 20260609_refactor_translations
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "20260609_drop_unused_tables"
down_revision = "20260609_refactor_translations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("cardmarket_product_localizations")
    op.drop_table("card_pricing_rules")


def downgrade() -> None:
    op.create_table(
        "card_pricing_rules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("scryfall_id", sa.String(), nullable=False),
        sa.Column("scryfall_oracle_id", sa.String(), nullable=False),
        sa.Column("catalog_printing_id", sa.String(), nullable=False),
        sa.Column("cardmarket_product_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("condition", sa.String(), nullable=True),
        sa.Column("language", sa.String(), nullable=True),
        sa.Column("foil", sa.Boolean(), nullable=True),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("manual_fixed_price", sa.Float(), nullable=True),
        sa.Column("manual_percentage", sa.Float(), nullable=True),
        sa.Column("reference_price_type", sa.String(), nullable=True),
        sa.Column("minimum_price", sa.Float(), nullable=True),
        sa.Column("baseline_under_price", sa.Float(), nullable=True),
        sa.Column("is_buyable", sa.Boolean(), nullable=False),
        sa.Column("contact_relic", sa.Boolean(), nullable=False),
        sa.Column("highlighted", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("end_date", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cardmarket_product_localizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id_product", sa.BigInteger(), nullable=False),
        sa.Column("id_language", sa.Integer(), nullable=False),
        sa.Column("language_name", sa.String(50), nullable=True),
        sa.Column("product_name", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["id_product"], ["cardmarket_products.id_product"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id_product", "id_language", name="uq_cm_product_localizations"),
    )
