"""
Table mtg_sets — Éditions Magic: The Gathering.
Source : Scryfall bulk data.
Clé métier : code (ex: "bro", "ltr", "mkm")
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Date, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card_printing import CardPrinting


class MtgSet(Base):
    __tablename__ = "mtg_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    set_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    released_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    block: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    parent_set_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    card_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    icon_svg_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    printings: Mapped[List["CardPrinting"]] = relationship(
        "CardPrinting", back_populates="mtg_set", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<MtgSet code={self.code!r} name={self.name!r}>"
