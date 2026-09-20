"""
Table card_printings — Impression physique précise d'une carte.
Clé métier : scryfall_id (UUID unique par impression dans Scryfall).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card import Card
    from mtgdb.db.models.card_price import CardPrice
    from mtgdb.db.models.mtg_set import MtgSet


class CardPrinting(Base):
    __tablename__ = "scryfall_card_printings"

    # Index GIN trigram servant les recherches ILIKE à joker de RELIC-Trade sur
    # le nom traduit. Déclaré ici — et pas seulement dans la migration
    # 20260824_printed_name_trgm — pour que --autogenerate le reconnaisse : un
    # index créé en migration mais absent des modèles se voit proposer à la
    # suppression à chaque génération. Le nom doit rester rigoureusement
    # identique à celui de la migration.
    #
    # Volontairement NON partiel : un `WHERE printed_name IS NOT NULL` ne gagne
    # que 0,9 % de taille (GIN n'indexe pas les NULL de toute façon) et obligerait
    # le planner à prouver l'implication du prédicat pour chaque requête. Voir
    # docs/recherche_trigram.md.
    #
    # Index fonctionnel sur lower(printed_name) : sert la résolution de decklist
    # de RELIC-Trade (deck_resolution.py:114 et :401), qui compare en minuscules.
    # Un index sur la colonne brute lui est inaccessible — c'est une expression.
    __table_args__ = (
        Index(
            "ix_scryfall_card_printings_printed_name_trgm",
            "printed_name",
            postgresql_using="gin",
            postgresql_ops={"printed_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_scryfall_card_printings_printed_name_lower",
            text("lower(printed_name)"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scryfall_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )
    oracle_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scryfall_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    set_code: Mapped[Optional[str]] = mapped_column(
        String(16),
        ForeignKey("scryfall_mtg_sets.code", ondelete="SET NULL"),
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
    # Qualité du visuel, telle que Scryfall la déclare : `highres_scan`, `lowres`,
    # `placeholder` ou `missing`.
    #
    # ⚠️ Sans elle, RIEN ne distingue un vrai scan d'un carton « Localized Image
    # Not Available » : les trois colonnes ci-dessus sont renseignées dans les
    # deux cas, et l'URL répond 200 avec une vraie image JPEG. Un consommateur qui
    # préfère l'impression d'une langue donnée — la vitrine de RELIC-Trade choisit
    # le visuel localisé — affiche alors le carton à la place de la carte, sans
    # aucun moyen de s'en apercevoir.
    image_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    scryfall_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cardmarket_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    tcgplayer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    tcgplayer_id_en: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    # Pas d'`index=True` : le btree sur la colonne brute ne servait aucune requête
    # des consommateurs (tous en ILIKE ou lower()). Supprimé par la migration
    # 20260824_printed_name_lower. Les deux index utiles sont en __table_args__.
    printed_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    card: Mapped["Card"] = relationship("Card", back_populates="printings")
    mtg_set: Mapped[Optional["MtgSet"]] = relationship("MtgSet", back_populates="printings")
    prices: Mapped[List["CardPrice"]] = relationship(
        "CardPrice", back_populates="printing", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<CardPrinting scryfall_id={self.scryfall_id!r} set={self.set_code!r}>"
