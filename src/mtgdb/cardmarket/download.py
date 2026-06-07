"""
Téléchargement intelligent des fichiers Cardmarket.
- Requête HEAD pour lire ETag / Last-Modified / Content-Length
- Comparaison avec le dernier import réussi
- Téléchargement conditionnel + calcul sha256
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session
from sqlalchemy import select

from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile

log = logging.getLogger("cardmarket.download")

HTTP_HEADERS = {"User-Agent": "MTG-DB/1.0 (educational project)"}


def _last_successful_import(session: Session, file_type: str) -> Optional[CardmarketImportFile]:
    return session.execute(
        select(CardmarketImportFile)
        .where(
            CardmarketImportFile.file_type == file_type,
            CardmarketImportFile.status == "success",
        )
        .order_by(CardmarketImportFile.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    client: httpx.Client,
    session: Session,
    url: str,
    file_type: str,
    dest_dir: Path,
) -> tuple[Optional[Path], CardmarketImportFile]:
    """
    Télécharge le fichier si nécessaire.
    Retourne (chemin_local, import_file_row).
    Si skipped, chemin_local est None.
    """
    now = datetime.now(timezone.utc)
    import_row = CardmarketImportFile(
        file_type=file_type,
        file_url=url,
        status="started",
        started_at=now,
    )
    session.add(import_row)
    session.flush()

    # HEAD pour lire les métadonnées
    try:
        head = client.head(url, headers=HTTP_HEADERS)
        head.raise_for_status()
        etag = head.headers.get("etag")
        last_modified = head.headers.get("last-modified")
        content_length = int(head.headers.get("content-length", 0)) or None
    except Exception as exc:
        log.warning(f"HEAD échoué sur {url} : {exc} — téléchargement forcé.")
        etag = last_modified = content_length = None

    import_row.etag = etag
    import_row.last_modified = last_modified
    import_row.content_length = content_length

    # Vérifier si le fichier a changé
    last = _last_successful_import(session, file_type)
    if last and etag and last.etag == etag:
        log.info(f"  [{file_type}] ETag identique — fichier non modifié, import ignoré.")
        import_row.status = "skipped_not_modified"
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        return None, import_row

    # Téléchargement
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = url.rsplit("/", 1)[-1].replace(".json", f"_{timestamp}.json")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    log.info(f"  [{file_type}] Téléchargement vers {dest} ...")
    try:
        with client.stream("GET", url, headers=HTTP_HEADERS, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    f.write(chunk)
    except Exception as exc:
        import_row.status = "failed"
        import_row.error_message = str(exc)
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise

    sha256 = _sha256_file(dest)
    import_row.local_file_path = str(dest)
    import_row.sha256 = sha256
    session.commit()

    log.info(f"  [{file_type}] Téléchargé ({dest.stat().st_size / 1_048_576:.1f} Mo) sha256={sha256[:12]}…")
    return dest, import_row
