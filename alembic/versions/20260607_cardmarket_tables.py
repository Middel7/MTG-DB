"""Cardmarket tables + cardmarket_id sur card_printings

Revision ID: 20260607_cardmarket_tables
Revises: a1b2c3d4e5f6
Create Date: 2026-06-07
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "20260607_cardmarket_tables"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cardmarket_products",
        sa.Column("id_product", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("id_metaproduct", sa.BigInteger(), nullable=True),
        sa.Column("count_reprints", sa.Integer(), nullable=True),
        sa.Column("en_name", sa.Text(), nullable=False),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("image", sa.Text(), nullable=True),
        sa.Column("game_name", sa.Text(), nullable=True),
        sa.Column("category_name", sa.Text(), nullable=True),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("expansion_name", sa.Text(), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_cardmarket_products_en_name", "cardmarket_products", ["en_name"])
    op.create_index("ix_cardmarket_products_expansion_name", "cardmarket_products", ["expansion_name"])
    op.create_index("ix_cardmarket_products_number", "cardmarket_products", ["number"])
    op.create_index("ix_cardmarket_products_id_metaproduct", "cardmarket_products", ["id_metaproduct"])

    op.create_table(
        "cardmarket_product_localizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id_product", sa.BigInteger(), sa.ForeignKey("cardmarket_products.id_product", ondelete="CASCADE"), nullable=False),
        sa.Column("id_language", sa.Integer(), nullable=False),
        sa.Column("language_name", sa.Text(), nullable=True),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.UniqueConstraint("id_product", "id_language", name="uq_cm_localization_product_language"),
    )
    op.create_index("ix_cm_localization_id_product", "cardmarket_product_localizations", ["id_product"])
    op.create_index("ix_cm_localization_product_name", "cardmarket_product_localizations", ["product_name"])
    op.create_index("ix_cm_localization_language_name", "cardmarket_product_localizations", ["language_name"])

    op.create_table(
        "cardmarket_import_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="cardmarket"),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("file_url", sa.Text(), nullable=False),
        sa.Column("local_file_path", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("content_length", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("file_type", "sha256", name="uq_cm_import_file_type_sha256"),
    )
    op.create_index("ix_cm_import_files_file_type", "cardmarket_import_files", ["file_type"])
    op.create_index("ix_cm_import_files_status", "cardmarket_import_files", ["status"])
    op.create_index("ix_cm_import_files_started_at", "cardmarket_import_files", ["started_at"])

    op.create_table(
        "cardmarket_price_guide_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("import_file_id", sa.Integer(), sa.ForeignKey("cardmarket_import_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("id_product", sa.BigInteger(), sa.ForeignKey("cardmarket_products.id_product", ondelete="SET NULL"), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("avg_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("low_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("trend_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("german_pro_low", sa.Numeric(12, 4), nullable=True),
        sa.Column("suggested_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_sell", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_low", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_trend", sa.Numeric(12, 4), nullable=True),
        sa.Column("low_price_ex_plus", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg1", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg7", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg30", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_avg1", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_avg7", sa.Numeric(12, 4), nullable=True),
        sa.Column("foil_avg30", sa.Numeric(12, 4), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False),
        sa.UniqueConstraint("import_file_id", "id_product", name="uq_cm_price_guide_import_product"),
    )
    op.create_index("ix_cm_price_guide_import_file_id", "cardmarket_price_guide_entries", ["import_file_id"])
    op.create_index("ix_cm_price_guide_id_product", "cardmarket_price_guide_entries", ["id_product"])
    op.create_index("ix_cm_price_guide_captured_at", "cardmarket_price_guide_entries", ["captured_at"])
    op.create_index("ix_cm_price_guide_product_captured", "cardmarket_price_guide_entries", ["id_product", "captured_at"])

    op.add_column("card_printings", sa.Column("cardmarket_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_card_printings_cardmarket_id", "card_printings", ["cardmarket_id"])

    op.execute("""
        CREATE VIEW v_cardmarket_latest_prices_by_printing AS
        SELECT
            cp.id              AS printing_id,
            cp.scryfall_id,
            c.name             AS card_name,
            cp.set_code,
            cp.collector_number,
            cp.lang,
            cmp.id_product,
            cmp.en_name,
            cmp.expansion_name,
            cmp.number,
            pge.low_price,
            pge.trend_price,
            pge.low_price_ex_plus,
            pge.avg1,
            pge.avg7,
            pge.avg30,
            pge.foil_low,
            pge.foil_trend,
            pge.foil_avg1,
            pge.foil_avg7,
            pge.foil_avg30,
            pge.captured_at
        FROM card_printings cp
        JOIN cards c ON c.id = cp.card_id
        JOIN cardmarket_products cmp ON cmp.id_product = cp.cardmarket_id
        JOIN LATERAL (
            SELECT * FROM cardmarket_price_guide_entries
            WHERE id_product = cp.cardmarket_id
            ORDER BY captured_at DESC
            LIMIT 1
        ) pge ON true
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_cardmarket_latest_prices_by_printing")

    op.drop_index("ix_card_printings_cardmarket_id", table_name="card_printings")
    op.drop_column("card_printings", "cardmarket_id")

    op.drop_index("ix_cm_price_guide_product_captured", table_name="cardmarket_price_guide_entries")
    op.drop_index("ix_cm_price_guide_captured_at", table_name="cardmarket_price_guide_entries")
    op.drop_index("ix_cm_price_guide_id_product", table_name="cardmarket_price_guide_entries")
    op.drop_index("ix_cm_price_guide_import_file_id", table_name="cardmarket_price_guide_entries")
    op.drop_table("cardmarket_price_guide_entries")

    op.drop_index("ix_cm_import_files_started_at", table_name="cardmarket_import_files")
    op.drop_index("ix_cm_import_files_status", table_name="cardmarket_import_files")
    op.drop_index("ix_cm_import_files_file_type", table_name="cardmarket_import_files")
    op.drop_table("cardmarket_import_files")

    op.drop_index("ix_cm_localization_language_name", table_name="cardmarket_product_localizations")
    op.drop_index("ix_cm_localization_product_name", table_name="cardmarket_product_localizations")
    op.drop_index("ix_cm_localization_id_product", table_name="cardmarket_product_localizations")
    op.drop_table("cardmarket_product_localizations")

    op.drop_index("ix_cardmarket_products_id_metaproduct", table_name="cardmarket_products")
    op.drop_index("ix_cardmarket_products_number", table_name="cardmarket_products")
    op.drop_index("ix_cardmarket_products_expansion_name", table_name="cardmarket_products")
    op.drop_index("ix_cardmarket_products_en_name", table_name="cardmarket_products")
    op.drop_table("cardmarket_products")
