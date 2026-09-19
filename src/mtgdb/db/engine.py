"""
Connexion SQLAlchemy à PostgreSQL.
Charge DATABASE_URL depuis le fichier .env à la racine du projet consommateur,
ou depuis la variable d'environnement si déjà définie.

Usage :
    from mtgdb.db.engine import SessionLocal, get_db, check_connection
"""
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from mtgdb.db.urls import (
    is_local_database_url,
    normalize_database_url,
    redact_database_url,
)
from mtgdb.runtime import env_flag, in_container

# Cherche un .env dans le répertoire courant ou ses parents
_cwd = Path.cwd()
for _parent in [_cwd, *_cwd.parents]:
    _dotenv = _parent / ".env"
    if _dotenv.exists():
        load_dotenv(_dotenv)
        break

# Render fournit encore des URL en postgres://, que SQLAlchemy 2 refuse. La
# correction était portée par update-prod.ps1, qui ne tourne plus sur le chemin
# de la production : elle doit avoir lieu ici, au seul endroit que tous les
# consommateurs traversent.
DATABASE_URL: Optional[str] = normalize_database_url(os.getenv("DATABASE_URL"))

if DATABASE_URL:
    engine: Optional[Engine] = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
else:
    engine = None
    SessionLocal = None  # type: ignore[assignment]


class LocalDatabaseRefused(RuntimeError):
    """La base visée est locale alors que le contexte exige une base distante."""


def get_db():
    """Générateur de session pour FastAPI (Depends)."""
    if SessionLocal is None:
        raise RuntimeError(
            "DATABASE_URL absent. "
            "Crée un fichier .env avec DATABASE_URL=postgresql://user:pass@host:port/dbname"
        )
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def assert_remote_database(url: Optional[str] = None) -> None:
    """
    Refuse une base locale lorsqu'on tourne en conteneur. Lève LocalDatabaseRefused.

    Ce garde-fou vient d'`update-prod.ps1` : sans lui, un run « production » mal
    configuré met à jour la base de développement et rend un rapport tout vert.
    En conteneur le diagnostic est encore plus certain qu'il ne l'était sur le
    poste — `localhost`, vu depuis un Cron Job Render, ne désigne rien d'autre
    que le conteneur lui-même.

    Volontairement appelée par les points d'entrée (scripts/update_all.py), et
    non à l'import du module : `mtgdb.db.engine` est une bibliothèque, importée
    par ManaMind_AI et RELIC-Trade. Une exception levée à l'import en casserait
    le démarrage pour une raison qui ne les concerne pas.

    Deux façons d'activer le garde-fou :

      - tourner en conteneur, détecté par `mtgdb.runtime.in_container()` ;
      - poser MTGDB_REQUIRE_REMOTE_DB=1, ce que fait `update-prod.ps1` quand on
        lance un rattrapage vers la production depuis le poste. Sans cette
        seconde porte, le garde-fou disparaîtrait du seul chemin où il
        protégeait quelque chose avant le passage au cloud.

    MTGDB_ALLOW_LOCAL_DB=1 lève la restriction, pour le cas légitime d'un
    conteneur qui vise une base publiée sur l'hôte.
    """
    target = normalize_database_url(url) if url is not None else DATABASE_URL
    if not target:
        raise RuntimeError(
            "DATABASE_URL absent. En conteneur, fournis-la par l'environnement "
            "du service (jamais par un .env embarqué dans l'image)."
        )
    enforced = in_container() or env_flag("MTGDB_REQUIRE_REMOTE_DB")
    if not enforced or env_flag("MTGDB_ALLOW_LOCAL_DB"):
        return
    if is_local_database_url(target):
        contexte = "ce run tourne en conteneur" if in_container() else                    "ce run exige une base distante (MTGDB_REQUIRE_REMOTE_DB)"
        raise LocalDatabaseRefused(
            f"DATABASE_URL pointe sur une base locale ({redact_database_url(target)}) "
            f"alors que {contexte} : ce n'est pas la production. "
            f"Abandon avant toute écriture. "
            f"(MTGDB_ALLOW_LOCAL_DB=1 pour passer outre en connaissance de cause.)"
        )


def check_connection() -> bool:
    """Vérifie que la base est accessible. Retourne True si OK, False sinon."""
    if engine is None:
        print("DATABASE_URL absent — connexion impossible.")
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        print(f"Connexion échouée : {exc}")
        return False
