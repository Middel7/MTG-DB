"""
Table scryfall_card_tags — Tags oracle (ORACLE_CARD_TAG) issus de Scryfall Tagger.
Rattachés à la carte logique (scryfall_cards), indépendants des impressions.
Source : tagger.scryfall.com via API GraphQL non officielle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card import Card


class CardTag(Base):
    __tablename__ = "scryfall_card_tags"
    __table_args__ = (UniqueConstraint("card_id", "tag_name", name="uq_card_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scryfall_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    card: Mapped["Card"] = relationship("Card", back_populates="tags")

    def __repr__(self) -> str:
        return f"<CardTag card_id={self.card_id} tag={self.tag_name!r}>"
