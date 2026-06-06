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

# Cherche un .env dans le répertoire courant ou ses parents
_cwd = Path.cwd()
for _parent in [_cwd, *_cwd.parents]:
    _dotenv = _parent / ".env"
    if _dotenv.exists():
        load_dotenv(_dotenv)
        break

DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

if DATABASE_URL:
    engine: Optional[Engine] = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
else:
    engine = None
    SessionLocal = None  # type: ignore[assignment]


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
