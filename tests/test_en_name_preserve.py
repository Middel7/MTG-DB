"""
Un nom de produit Cardmarket déjà connu ne doit pas être effacé par un export appauvri.

`parse_product()` retombe sur `default=""` quand Cardmarket ne fournit ni
`enName`, ni `en_name`, ni `name`. La colonne `cardmarket_products.en_name` est
`NOT NULL` mais sans contrainte de non-vacuité : la chaîne vide passe sans bruit,
et l'upsert écrasait alors un nom déjà correct. Un export appauvri d'une seule
journée suffisait à vider le catalogue, sans erreur et sans trace.

Défaut signalé par l'audit RELIC-Trade du 19/09/2026. Il est sans conséquence
pour cette application — elle ne lit jamais `en_name`, seulement `website`, et le
pont vers Cardmarket passe par `id_product` — mais c'est une perte de donnée dans
le catalogue, et elle est irréversible sans réimport complet.

Le sens inverse doit continuer de fonctionner : un produit reconstitué par
`import_price_guide` avec un nom vide reçoit bien son vrai nom au premier passage
du Product Catalog qui le contient.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
ID_TEST = 900_000_500


@pytest.fixture(scope="module")
def base_jetable(database_url):
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test crée une base : réservé à un PostgreSQL local.")

    nom = f"mtgdb_nom_{uuid.uuid4().hex[:12]}"
    racine = database_url.rsplit("/", 1)[0]
    url_test = f"{racine}/{nom}"

    admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{nom}"'))
    admin.dispose()

    try:
        import os

        resultat = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT), env={**os.environ, "DATABASE_URL": url_test},
            capture_output=True, text=True, timeout=300,
        )
        if resultat.returncode != 0:
            pytest.fail(f"migrations impossibles :\n{resultat.stderr[-2000:]}")
        yield url_test
    finally:
        admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :nom AND pid <> pg_backend_pid()"), {"nom": nom})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{nom}"'))
        admin.dispose()


@pytest.fixture
def session(base_jetable):
    from sqlalchemy.orm import sessionmaker

    moteur = create_engine(base_jetable)
    Session = sessionmaker(bind=moteur)
    with Session() as s:
        s.execute(text("TRUNCATE cardmarket_price_guide_entries, "
                       "cardmarket_import_files, cardmarket_products CASCADE"))
        s.commit()
        yield s
    moteur.dispose()


def _catalogue(tmp_path, produits: list[dict], nom_fichier: str):
    chemin = tmp_path / nom_fichier
    chemin.write_text(json.dumps(produits), encoding="utf-8")
    return chemin


def _import(session):
    from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile

    ligne = CardmarketImportFile(
        file_type="test_catalogue", file_url="test://catalogue",
        status="started", started_at=datetime.now(timezone.utc))
    session.add(ligne)
    session.commit()
    session.refresh(ligne)
    return ligne


def _nom(session) -> str:
    return session.execute(
        text("SELECT en_name FROM cardmarket_products WHERE id_product = :p"),
        {"p": ID_TEST}).scalar()


@pytest.mark.integration
def test_un_export_sans_nom_n_efface_pas_le_nom_connu(session, tmp_path):
    """Le cas signalé : une journée d'export appauvri ne doit rien détruire."""
    from mtgdb.cardmarket.import_product_catalog import import_product_catalog

    complet = _catalogue(tmp_path, [{"idProduct": ID_TEST, "enName": "Sol Ring"}], "1.json")
    import_product_catalog(complet, session, _import(session))
    assert _nom(session) == "Sol Ring"

    # Même produit, mais Cardmarket ne fournit plus aucun des trois champs de nom.
    appauvri = _catalogue(tmp_path, [{"idProduct": ID_TEST, "idExpansion": 1249}], "2.json")
    import_product_catalog(appauvri, session, _import(session))

    assert _nom(session) == "Sol Ring", "le nom connu a été écrasé par une chaîne vide"


@pytest.mark.integration
def test_un_nom_vide_est_bien_remplace_par_un_vrai_nom(session, tmp_path):
    """
    Le sens inverse, indispensable.

    `import_price_guide` crée des produits à `en_name` vide quand il rencontre un
    produit avant le Product Catalog. Ils doivent être renseignés au premier
    passage du catalogue qui les contient — sans quoi ils resteraient vides pour
    toujours.
    """
    from mtgdb.cardmarket.import_product_catalog import import_product_catalog

    session.execute(text("""
        INSERT INTO cardmarket_products (id_product, en_name, raw_json)
        VALUES (:p, '', '{"_source": "price_guide"}'::jsonb)
    """), {"p": ID_TEST})
    session.commit()

    catalogue = _catalogue(tmp_path, [{"idProduct": ID_TEST, "enName": "Sol Ring"}], "3.json")
    import_product_catalog(catalogue, session, _import(session))

    assert _nom(session) == "Sol Ring"


@pytest.mark.integration
def test_un_nom_qui_change_est_bien_mis_a_jour(session, tmp_path):
    """Préserver ne doit pas vouloir dire figer : Cardmarket corrige ses libellés."""
    from mtgdb.cardmarket.import_product_catalog import import_product_catalog

    premier = _catalogue(tmp_path, [{"idProduct": ID_TEST, "enName": "Sol Rng"}], "4.json")
    import_product_catalog(premier, session, _import(session))

    corrige = _catalogue(tmp_path, [{"idProduct": ID_TEST, "enName": "Sol Ring"}], "5.json")
    import_product_catalog(corrige, session, _import(session))

    assert _nom(session) == "Sol Ring"
