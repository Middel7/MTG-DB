from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.cardmarket_product import CardmarketProduct


class CardmarketProductLocalization(Base):
    __tablename__ = "cardmarket_product_localizations"

    __table_args__ = (
        UniqueConstraint("id_product", "id_language", name="uq_cm_localization_product_language"),
        Index("ix_cm_localization_product_name", "product_name"),
        Index("ix_cm_localization_language_name", "language_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_product: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cardmarket_products.id_product", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    id_language: Mapped[int] = mapped_column(Integer, nullable=False)
    language_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)

    product: Mapped["CardmarketProduct"] = relationship(
        "CardmarketProduct", back_populates="localizations"
    )

    def __repr__(self) -> str:
        return (
            f"<CardmarketProductLocalization id_product={self.id_product} "
            f"id_language={self.id_language} product_name={self.product_name!r}>"
        )
