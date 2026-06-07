from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from mtgdb.db.base import Base

if TYPE_CHECKING:
    from mtgdb.db.models.cardmarket_price_guide_entry import CardmarketPriceGuideEntry


class CardmarketImportFile(Base):
    __tablename__ = "cardmarket_import_files"

    __table_args__ = (
        UniqueConstraint("file_type", "sha256", name="uq_cm_import_file_type_sha256"),
        Index("ix_cm_import_files_file_type", "file_type"),
        Index("ix_cm_import_files_status", "status"),
        Index("ix_cm_import_files_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="cardmarket")
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    local_file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    etag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_modified: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_length: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="started")
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    price_guide_entries: Mapped[List["CardmarketPriceGuideEntry"]] = relationship(
        "CardmarketPriceGuideEntry", back_populates="import_file"
    )

    def __repr__(self) -> str:
        return (
            f"<CardmarketImportFile id={self.id} file_type={self.file_type!r} "
            f"status={self.status!r}>"
        )
