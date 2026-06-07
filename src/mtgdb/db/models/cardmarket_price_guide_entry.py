from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
    from mtgdb.db.models.cardmarket_product import CardmarketProduct


class CardmarketPriceGuideEntry(Base):
    __tablename__ = "cardmarket_price_guide_entries"

    __table_args__ = (
        UniqueConstraint(
            "import_file_id", "id_product", name="uq_cm_price_guide_import_product"
        ),
        Index("ix_cm_price_guide_id_product", "id_product"),
        Index("ix_cm_price_guide_captured_at", "captured_at"),
        Index("ix_cm_price_guide_product_captured", "id_product", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cardmarket_import_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    id_product: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("cardmarket_products.id_product", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    avg_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    low_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    trend_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    german_pro_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    suggested_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_sell: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_trend: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    low_price_ex_plus: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    avg1: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    avg7: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    avg30: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_avg1: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_avg7: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    foil_avg30: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)

    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    import_file: Mapped["CardmarketImportFile"] = relationship(
        "CardmarketImportFile", back_populates="price_guide_entries"
    )
    product: Mapped[Optional["CardmarketProduct"]] = relationship(
        "CardmarketProduct", back_populates="price_guide_entries"
    )

    def __repr__(self) -> str:
        return (
            f"<CardmarketPriceGuideEntry id={self.id} id_product={self.id_product} "
            f"captured_at={self.captured_at}>"
        )
