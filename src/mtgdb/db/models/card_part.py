"""
Table scryfall_card_parts — Cartes liées à une carte : jetons, emblèmes, moitiés
de fusion, pièces de combo.

Source : le champ `all_parts` du bulk Scryfall, qui donne pour chaque carte la
liste exacte des objets qu'elle met en jeu. C'est la seule donnée qui permette de
répondre à « quels jetons faut-il pour jouer ce deck ? » sans deviner : le texte
d'oracle dit « create a 1/1 white Soldier creature token », il ne dit pas
lequel des trois jetons Soldier blancs de Scryfall est le bon.

`all_parts` référence des IMPRESSIONS (`part_scryfall_id` pointe
`scryfall_card_printings.scryfall_id`), pas des cartes : un même jeton imprimé
dans deux éditions porte deux identifiants. Remonter à l'oracle du jeton se fait
par jointure. `part_name` et `part_type_line` sont recopiés du bulk pour que la
lecture reste possible même si l'impression citée manque du catalogue.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.card import Card


class CardPart(Base):
    __tablename__ = "scryfall_card_parts"
    __table_args__ = (
        UniqueConstraint(
            "card_id", "part_scryfall_id", "component", name="uq_card_parts_carte_part"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scryfall_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'token', 'meld_part', 'meld_result' ou 'combo_piece'. Seul 'token' répond à
    # la question des jetons ; les autres sont conservés parce qu'ils viennent du
    # même champ et qu'un second import pour aller les chercher n'aurait pas de sens.
    component: Mapped[str] = mapped_column(String(20), nullable=False)
    part_scryfall_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    part_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    part_type_line: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    card: Mapped["Card"] = relationship("Card", back_populates="parts")

    def __repr__(self) -> str:
        return (f"<CardPart card_id={self.card_id} component={self.component!r} "
                f"name={self.part_name!r}>")
