"""
Table card_faces — Faces des cartes double-face (DFC), split, adventure, etc.
Seulement créées si le JSON Scryfall contient un tableau "card_faces".
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card import Card


class CardFace(Base):
    __tablename__ = "scryfall_card_faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scryfall_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    face_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mana_cost: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    type_line: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    oracle_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    power: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    toughness: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    loyalty: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    defense: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    colors: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    image_small: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_normal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_large: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    card: Mapped["Card"] = relationship("Card", back_populates="faces")

    def __repr__(self) -> str:
        return f"<CardFace card_id={self.card_id} face_name={self.face_name!r}>"
