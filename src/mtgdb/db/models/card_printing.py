"""
Table card_printings — Impression physique précise d'une carte.
Clé métier : scryfall_id (UUID unique par impression dans Scryfall).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card import Card
    from mtgdb.db.models.card_price import CardPrice
    from mtgdb.db.models.mtg_set import MtgSet


class CardPrinting(Base):
    __tablename__ = "card_printings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scryfall_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )
    oracle_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    set_code: Mapped[Optional[str]] = mapped_column(
        String(16),
        ForeignKey("mtg_sets.code", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    collector_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lang: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    rarity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    released_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    artist: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    border_color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    frame: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    full_art: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    promo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reprint: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    digital: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_small: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_normal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_large: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scryfall_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    card: Mapped["Card"] = relationship("Card", back_populates="printings")
    mtg_set: Mapped[Optional["MtgSet"]] = relationship("MtgSet", back_populates="printings")
    prices: Mapped[List["CardPrice"]] = relationship(
        "CardPrice", back_populates="printing", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<CardPrinting scryfall_id={self.scryfall_id!r} set={self.set_code!r}>"
