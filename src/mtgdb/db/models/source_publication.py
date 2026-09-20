"""
Table mtgdb_source_publications — ce que les sources amont ont publié, et quand.

`import_runs` répond à « quand MTG-DB a-t-il été mis à jour ». Elle ne répond
pas à « quand la source a-t-elle publié », ni surtout à « depuis combien de
temps une version publiée attend-elle d'être absorbée ».

Une ligne par VERSION publiée, pas par vérification : le pipeline passe toutes
les heures, mais Scryfall ne publie que deux fois par jour et Cardmarket une
seule. Enregistrer chaque passage produirait 24 lignes quotidiennes de bruit
pour deux informations utiles.

Le préfixe `mtgdb_` est délibéré : la base de production est partagée avec
RELIC-Trade, et un nom aussi générique que `source_publications` s'y heurterait
tôt ou tard.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mtgdb.db.base import Base

# Identifiants de source. Constantes plutôt qu'énumération SQL : ajouter une
# source ne doit pas demander une migration sur une base partagée.
SOURCE_SCRYFALL_BULK = "scryfall_bulk"
SOURCE_CARDMARKET_PRICE_GUIDE = "cardmarket_price_guide"
SOURCE_CARDMARKET_PRODUCT_CATALOG = "cardmarket_product_catalog"


class SourcePublication(Base):
    __tablename__ = "mtgdb_source_publications"

    __table_args__ = (
        # Le couple (source, version) EST l'identité d'une publication. C'est
        # cette contrainte qui rend l'enregistrement idempotent : un passage
        # horaire qui revoit la même version ne crée pas de ligne.
        UniqueConstraint("source", "version", name="uq_source_publications_source_version"),
        Index("ix_source_publications_source_published", "source", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(50), nullable=False)

    # Nom de fichier du bulk Scryfall, ou ETag Cardmarket. C'est ce que la source
    # nous donne pour distinguer deux versions.
    version: Mapped[str] = mapped_column(Text, nullable=False)

    # Date annoncée par la source. Nullable : un HEAD qui échoue laisse le
    # téléchargement se faire sans en-tête `Last-Modified`.
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Quand NOUS l'avons vue. L'écart avec `published_at` mesure la réactivité de
    # la veille ; l'écart avec `imported_at` mesure celle de l'import.
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Dernière fois que le pipeline a INTERROGÉ cette source, qu'elle ait publié
    # du neuf ou non. Sans cette colonne, rien ne distingue « la source est
    # calme » de « le cron ne tourne plus » : dans les deux cas, aucune nouvelle
    # ligne n'apparaît.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Renseigné à la fin d'un import réussi. Reste NULL tant que la version
    # n'a pas été absorbée — c'est ce qui permet de détecter un décrochage.
    imported_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        etat = "importée" if self.imported_at else "en attente"
        return f"<SourcePublication {self.source} {self.version!r} {etat}>"
