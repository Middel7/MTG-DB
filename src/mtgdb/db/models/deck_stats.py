"""
Tables de statistiques de decklists Commander.

Trois tables :
  deck_stat_global      — fréquence globale de chaque carte tous commandants confondus
  deck_stat_commander   — fréquence d'une carte pour un commandant donné
  deck_stat_idf         — IDF de chaque carte (discriminance inter-commandants)

Toutes les tables sont reconstruites par le script compute_deck_stats.py (TRUNCATE + INSERT).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mtgdb.db.base import Base


class DeckStatGlobal(Base):
    """Fréquence globale d'une carte parmi tous les decks de tous commandants."""

    __tablename__ = "deck_stat_global"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    decks_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, doc="Nb de decks distincts contenant la carte"
    )
    total_decks: Mapped[int] = mapped_column(
        BigInteger, nullable=False, doc="Nb total de decks dans le dataset"
    )
    global_frequency: Mapped[float] = mapped_column(
        Float, nullable=False, doc="decks_count / total_decks × 100 (pourcentage)"
    )
    commanders_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        doc="Nb de commandants distincts dont des decks jouent cette carte"
    )
    idf: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0,
        doc="log(nb_total_commandants / nb_commandants_jouant_carte)"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<DeckStatGlobal {self.card_name!r} {self.global_frequency:.1f}%>"


class DeckStatCommander(Base):
    """Fréquence d'une carte pour un commandant précis."""

    __tablename__ = "deck_stat_commander"

    __table_args__ = (
        UniqueConstraint("commander", "card_name", name="uq_deck_stat_commander_card"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    commander: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    card_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    decks_with_card: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Nb de decks de CE commandant contenant la carte"
    )
    total_decks: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Nb total de decks pour CE commandant"
    )
    inclusion_rate: Mapped[float] = mapped_column(
        Float, nullable=False, doc="decks_with_card / total_decks × 100 (pourcentage)"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<DeckStatCommander {self.commander!r} {self.card_name!r} "
            f"{self.inclusion_rate:.1f}%>"
        )
