"""
Table cards — Carte logique (niveau oracle), indépendante des éditions/reprints.
Un oracle_id = une seule entrée, peu importe le nombre d'impressions.
Source : Scryfall bulk data (champ oracle_id).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card_face import CardFace
    from mtgdb.db.models.card_printing import CardPrinting
    from mtgdb.db.models.card_tag import CardTag


def normalize_card_name(name: str) -> str:
    """
    Normalise un nom de carte pour la recherche :
    - minuscules
    - suppression des accents (NFD → ASCII)
    - suppression des espaces superflus
    - conserve le '//' des cartes split (ex: "Fire // Ice" → "fire // ice")
    """
    nfd = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return ascii_only.strip().lower()


class Card(Base):
    __tablename__ = "scryfall_cards"

    # Index PARTIEL : la sélection des cartes à taguer ne s'intéresse qu'à celles
    # déjà vérifiées, pour les écarter. Les NULL n'ont rien à faire dans l'index.
    __table_args__ = (
        Index(
            "ix_scryfall_cards_tagger_checked_at",
            "tagger_checked_at",
            postgresql_where=text("tagger_checked_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oracle_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    mana_cost: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    mana_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    type_line: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    oracle_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    power: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    toughness: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    loyalty: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    defense: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    colors: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    color_identity: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    keywords: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    legal_commander: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    edhrec_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    game_changer: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false", index=True
    )
    # Date du dernier passage de Scryfall Tagger sur cette carte, qu'il ait produit
    # des tags ou non. Sans elle, une carte que Tagger connaît mais n'a taguée avec
    # rien reste « sans tag » et se voit réinterrogée à chaque run hebdomadaire,
    # indéfiniment — une requête HTTP et 0,2 s de pause à chaque fois.
    tagger_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    faces: Mapped[List["CardFace"]] = relationship(
        "CardFace", back_populates="card", cascade="all, delete-orphan", lazy="select"
    )
    printings: Mapped[List["CardPrinting"]] = relationship(
        "CardPrinting", back_populates="card", lazy="select"
    )
    tags: Mapped[List["CardTag"]] = relationship(
        "CardTag", back_populates="card", cascade="all, delete-orphan", lazy="select"
    )
    def __repr__(self) -> str:
        return f"<Card oracle_id={self.oracle_id!r} name={self.name!r}>"
