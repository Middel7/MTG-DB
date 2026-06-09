"""
Table card_prices — Historique des prix par impression.
Append-only : on insère une nouvelle ligne par import (pas de mise à jour).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card_printing import CardPrinting


class CardPrice(Base):
    __tablename__ = "scryfall_card_prices"

    __table_args__ = (
        UniqueConstraint(
            "printing_id", "date", "source", "currency", "price_type",
            name="uq_card_prices_printing_date_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    printing_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scryfall_card_printings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="scryfall")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_type: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    printing: Mapped["CardPrinting"] = relationship("CardPrinting", back_populates="prices")

    def __repr__(self) -> str:
        return (
            f"<CardPrice printing_id={self.printing_id} "
            f"{self.currency} {self.price_type}={self.price}>"
        )
