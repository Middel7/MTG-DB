"""
Fixtures communes.

Les tests d'intégration ont besoin d'une vraie base PostgreSQL : un verrou
consultatif ne se simule pas utilement, c'est le serveur qui l'arbitre. Ils se
sautent d'eux-mêmes quand DATABASE_URL est absent, pour que `pytest` reste
exécutable sur une machine nue.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@pytest.fixture(scope="session")
def database_url() -> str:
    from mtgdb.db.urls import normalize_database_url

    url = normalize_database_url(os.getenv("DATABASE_URL"))
    if not url:
        pytest.skip("DATABASE_URL absent : test d'intégration sauté.")
    return url


@pytest.fixture(autouse=True)
def _environnement_neutre(monkeypatch):
    """
    Neutralise les variables qui pilotent les garde-fous.

    Sans cela, un test hériterait de l'environnement du shell — et passerait ou
    échouerait selon la machine, ce qui est la pire forme d'échec.
    """
    for name in ("MTGDB_CONTAINER", "MTGDB_REQUIRE_REMOTE_DB", "MTGDB_ALLOW_LOCAL_DB", "RENDER"):
        monkeypatch.delenv(name, raising=False)

@pytest.fixture
def verrou_libre(database_url):
    """
    Saute le test si un run detient deja le verrou anti-chevauchement.

    Les tests du verrou supposent qu'aucun import ne tourne. Lances pendant un
    run — ce qui arrive des qu'on developpe et qu'on valide en meme temps — ils
    echouaient tous les six, sur un message qui n'indiquait pas la vraie cause.
    Un saut explicite vaut mieux qu'un echec trompeur.
    """
    from sqlalchemy import create_engine, text

    from mtgdb.db.lock import UPDATE_ALL_LOCK_KEY

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            tenu = conn.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND (classid::bigint << 32) | objid::bigint = :k"),
                {"k": UPDATE_ALL_LOCK_KEY}).scalar()
    finally:
        engine.dispose()

    if tenu:
        pytest.skip("Un import tient le verrou : test du verrou sauté.")
    return database_url
