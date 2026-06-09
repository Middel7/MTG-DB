from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.cardmarket_price_guide_entry import CardmarketPriceGuideEntry


class CardmarketProduct(Base):
    __tablename__ = "cardmarket_products"

    __table_args__ = (
        Index("ix_cardmarket_products_en_name", "en_name"),
        Index("ix_cardmarket_products_expansion_name", "expansion_name"),
        Index("ix_cardmarket_products_number", "number"),
        Index("ix_cardmarket_products_id_metaproduct", "id_metaproduct"),
    )

    id_product: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    id_metaproduct: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    count_reprints: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    en_name: Mapped[str] = mapped_column(Text, nullable=False)
    website: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    game_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rarity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expansion_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    price_guide_entries: Mapped[List["CardmarketPriceGuideEntry"]] = relationship(
        "CardmarketPriceGuideEntry", back_populates="product"
    )

    def __repr__(self) -> str:
        return f"<CardmarketProduct id_product={self.id_product} en_name={self.en_name!r}>"
