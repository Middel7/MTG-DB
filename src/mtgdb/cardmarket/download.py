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
from sqlalchemy import select
from sqlalchemy.orm import Session

from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile
from mtgdb.db.publications import (
    SOURCES_PAR_FILE_TYPE,
    enregistrer_publication,
    marquer_publication_importee,
    parser_date_http,
)

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


def marquer_imports_orphelins(session: Session, older_than_hours: int = 6) -> int:
    """
    Marque `failed` les imports Cardmarket restés `started`.

    Symétrique de `mtgdb.db.runs.marquer_runs_orphelins`, qui n'existait que pour
    Scryfall. Un import interrompu — conteneur arrêté, base injoignable — laissait
    sa ligne en `started` indéfiniment : une du 07/06/2026 traînait encore trois
    mois plus tard. Sans conséquence sur les données, puisque
    `_last_successful_import` ne regarde que les `success`, mais l'historique
    devenait illisible et cette table inutilisable pour superviser quoi que ce soit.
    """
    from sqlalchemy import text as sa_text

    resultat = session.execute(sa_text("""
        UPDATE cardmarket_import_files
           SET status = 'failed',
               finished_at = now(),
               error_message = COALESCE(
                   error_message,
                   'Import orphelin : processus interrompu avant la fin. '
                   'Marqué par un import ultérieur.')
         WHERE status = 'started'
           AND started_at < now() - make_interval(hours => :heures)
    """), {"heures": older_than_hours})
    session.commit()
    return resultat.rowcount


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

    # Suivi des publications amont. L'ETag est l'identifiant de version que
    # Cardmarket nous donne ; `last_modified` arrive en texte HTTP (« Sun, 20 Sep
    # 2026 00:42:36 GMT ») et n'est exploitable qu'une fois converti.
    source_suivi = SOURCES_PAR_FILE_TYPE.get(file_type)
    if source_suivi and etag:
        enregistrer_publication(source_suivi, etag, parser_date_http(last_modified))

    # Vérifier si le fichier a changé
    last = _last_successful_import(session, file_type)
    if last and etag and last.etag == etag:
        log.info(f"  [{file_type}] ETag identique — fichier non modifié, import ignoré.")
        import_row.status = "skipped_not_modified"
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        # Ce statut signifie exactement « la version publiée est déjà en base ».
        # Le suivi doit donc la considérer absorbée, sinon la première exécution
        # suivant la création de la table afficherait un retard imaginaire sur
        # une version importée de longue date.
        if source_suivi:
            marquer_publication_importee(source_suivi, etag)
        return None, import_row

    # Téléchargement
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = url.rsplit("/", 1)[-1].replace(".json", f"_{timestamp}.json")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    # Écriture dans un fichier temporaire, renommé une fois complet. Sans cela,
    # une interruption en cours de transfert laisse un JSON tronqué portant un nom
    # parfaitement valide : le run suivant le prend pour un export complet, et le
    # diagnostic part sur « Cardmarket nous envoie du JSON corrompu ».
    # `os.replace` est atomique sur le même système de fichiers.
    partiel = dest.with_suffix(dest.suffix + ".part")

    log.info(f"  [{file_type}] Téléchargement vers {dest} ...")
    try:
        with client.stream("GET", url, headers=HTTP_HEADERS, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(partiel, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    f.write(chunk)
    except Exception as exc:
        partiel.unlink(missing_ok=True)
        import_row.status = "failed"
        import_row.error_message = str(exc)
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise

    # Contrôle de complétude quand le serveur a annoncé une taille. Un transfert
    # coupé net après un `raise_for_status()` réussi ne lève rien par lui-même.
    taille = partiel.stat().st_size
    if content_length and taille != content_length:
        partiel.unlink(missing_ok=True)
        message = (f"téléchargement incomplet : {taille:,} octets reçus sur "
                   f"{content_length:,} annoncés")
        import_row.status = "failed"
        import_row.error_message = message
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise OSError(f"[{file_type}] {message}")

    sha256 = _sha256_file(partiel)

    # Le sha256 était calculé puis jamais comparé, alors qu'il porte une contrainte
    # d'unicité `(file_type, sha256)`. Deux conséquences : la déduplication qu'il
    # permettait n'existait pas, et un contenu identique à un import réussi
    # précédent — cas atteint dès que le HEAD échoue et que la comparaison d'ETag
    # est sautée — faisait lever une IntegrityError non capturée, en plein milieu
    # du chemin nominal dégradé.
    if last and last.sha256 == sha256:
        log.info(f"  [{file_type}] Contenu identique au dernier import réussi "
                 f"(sha256={sha256[:12]}…) — import ignoré.")
        partiel.unlink(missing_ok=True)
        import_row.status = "skipped_not_modified"
        import_row.finished_at = datetime.now(timezone.utc)
        session.commit()
        return None, import_row

    partiel.replace(dest)
    import_row.local_file_path = str(dest)
    import_row.sha256 = sha256
    session.commit()

    log.info(f"  [{file_type}] Téléchargé ({dest.stat().st_size / 1_048_576:.1f} Mo) "
             f"sha256={sha256[:12]}…")
    return dest, import_row
