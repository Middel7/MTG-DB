"""
Réparation des prix Cardmarket orphelins, sur une base JETABLE.

Ce script est destiné à tourner une fois sur la production, sur 252 414 lignes
d'une table de 3,4 Go lue par RELIC-Trade. Il modifie des données historiques et
crée des lignes dans `cardmarket_products`. Le tester n'est pas une précaution de
confort.

POURQUOI UNE BASE JETABLE, ET NON LA BASE LOCALE
`creer_produits_manquants()` et `rattacher()` opèrent sur la TABLE ENTIÈRE — c'est
leur raison d'être. Les appeler depuis un test branché sur `manamind` répare donc
toute la base, y compris les lignes qui n'appartiennent pas au test. C'est
exactement ce qui s'est produit le 19/09/2026 en écrivant ces tests : les 252 414
lignes historiques ont été rattachées et 5 085 produits créés, sans que personne
l'ait demandé.

Un script dont la portée est globale ne peut pas être testé sur une base
partagée. Ces tests créent donc leur propre base, y jouent les migrations, et la
détruisent à la fin. Quelques secondes de plus, et aucune surprise possible.

Les trois propriétés à garantir :

  1. `--dry-run` n'écrit rien — c'est le seul moyen d'évaluer l'ampleur avant
     d'agir ;
  2. la réparation rattache le prix au produit que `raw_json` nomme ;
  3. elle est rejouable : sur 252 414 lignes et une instance à 0,1 vCPU,
     l'interruption n'est pas une hypothèse d'école.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def reparation():
    spec = importlib.util.spec_from_file_location(
        "reparer_prix_orphelins", ROOT / "scripts" / "reparer_prix_orphelins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def base_jetable(database_url):
    """
    Crée une base neuve, y applique les migrations, la détruit à la fin.

    Refuse de tourner ailleurs qu'en local : créer une base est une opération
    d'administration, pas quelque chose qu'on lance par inadvertance sur un
    serveur d'hébergement.
    """
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test crée une base : réservé à un PostgreSQL local.")

    nom = f"mtgdb_test_{uuid.uuid4().hex[:12]}"
    racine = database_url.rsplit("/", 1)[0]
    url_test = f"{racine}/{nom}"

    admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{nom}"'))
    admin.dispose()

    try:
        resultat = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT),
            env={**_environnement_sans_dotenv(), "DATABASE_URL": url_test},
            capture_output=True, text=True, timeout=300,
        )
        if resultat.returncode != 0:
            pytest.fail(f"migrations impossibles sur la base de test :\n{resultat.stderr[-2000:]}")
        yield url_test
    finally:
        admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            # Couper les connexions résiduelles, sinon le DROP reste bloqué.
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :nom AND pid <> pg_backend_pid()"), {"nom": nom})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{nom}"'))
        admin.dispose()


def _environnement_sans_dotenv() -> dict:
    """Environnement courant, débarrassé de la DATABASE_URL du poste."""
    import os
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    return env


@pytest.fixture
def session(base_jetable):
    from sqlalchemy.orm import sessionmaker

    moteur = create_engine(base_jetable)
    Session = sessionmaker(bind=moteur)
    with Session() as s:
        # Chaque test repart d'une table vide : les fonctions testées ont une
        # portée globale, l'isolation doit donc venir de la base elle-même.
        s.execute(text("TRUNCATE cardmarket_price_guide_entries, "
                       "cardmarket_import_files, cardmarket_products CASCADE"))
        s.commit()
        yield s
    moteur.dispose()


def _creer_orpheline(session, id_produit: int, produit_existe: bool) -> int:
    """Insère un prix à `id_product` NULL, comme l'ancien import en produisait."""
    fichier = session.execute(text("""
        INSERT INTO cardmarket_import_files (file_type, file_url, status, started_at)
        VALUES ('test_reparation', 'test://reparation', 'success', :debut)
        RETURNING id
    """), {"debut": datetime.now(timezone.utc)}).scalar_one()

    if produit_existe:
        session.execute(text("""
            INSERT INTO cardmarket_products (id_product, en_name, raw_json)
            VALUES (:pid, 'Produit de test', '{}'::jsonb)
            ON CONFLICT (id_product) DO NOTHING
        """), {"pid": id_produit})

    ligne = session.execute(text("""
        INSERT INTO cardmarket_price_guide_entries
               (import_file_id, id_product, captured_at, avg_price, raw_json)
        VALUES (:fid, NULL, now(), 1.23,
                jsonb_build_object('idProduct', :pid, 'avg', 1.23))
        RETURNING id
    """), {"fid": fichier, "pid": id_produit}).scalar_one()
    session.commit()
    return ligne


@pytest.mark.integration
def test_le_dry_run_n_ecrit_rien(reparation, session):
    """
    La propriété la plus importante avant une exécution en production : c'est le
    seul moyen d'évaluer l'ampleur du chantier tant que rien n'est engagé.
    """
    ligne = _creer_orpheline(session, 5001, produit_existe=True)
    produits_avant = session.execute(
        text("SELECT count(*) FROM cardmarket_products")).scalar_one()

    reparation.creer_produits_manquants(session, dry_run=True)

    assert session.execute(
        text("SELECT id_product FROM cardmarket_price_guide_entries WHERE id = :i"),
        {"i": ligne}).scalar() is None, "le dry-run a modifié une ligne"
    assert session.execute(
        text("SELECT count(*) FROM cardmarket_products")).scalar_one() == produits_avant


@pytest.mark.integration
def test_un_prix_dont_le_produit_existe_est_rattache(reparation, session):
    """Le cas simple : le produit est arrivé depuis, il suffit de relier."""
    ligne = _creer_orpheline(session, 5002, produit_existe=True)

    reparation.rattacher(session, taille_lot=1000)

    assert session.execute(
        text("SELECT id_product FROM cardmarket_price_guide_entries WHERE id = :i"),
        {"i": ligne}).scalar() == 5002


@pytest.mark.integration
def test_un_produit_disparu_du_catalogue_est_recree(reparation, session):
    """
    Le cas majoritaire : 251 890 des 252 414 lignes, pour 5 085 produits.

    Sans recréation, ces prix resteraient inexploitables — l'information ne vit
    plus que dans `raw_json`.
    """
    ligne = _creer_orpheline(session, 5003, produit_existe=False)

    reparation.creer_produits_manquants(session, dry_run=False)
    reparation.rattacher(session, taille_lot=1000)

    assert session.execute(
        text("SELECT id_product FROM cardmarket_price_guide_entries WHERE id = :i"),
        {"i": ligne}).scalar() == 5003
    assert session.execute(
        text("SELECT en_name FROM cardmarket_products WHERE id_product = 5003")
    ).scalar() == "", (
        "le produit reconstitué porte un nom vide : c'est le signal qu'il attend "
        "d'être renseigné par le Product Catalog"
    )


@pytest.mark.integration
def test_la_reparation_est_rejouable(reparation, session):
    """Un script de maintenance interrompu doit pouvoir être relancé."""
    _creer_orpheline(session, 5004, produit_existe=False)

    reparation.creer_produits_manquants(session, dry_run=False)
    premier = reparation.rattacher(session, taille_lot=1000)

    reparation.creer_produits_manquants(session, dry_run=False)
    second = reparation.rattacher(session, taille_lot=1000)

    assert premier == 1
    assert second == 0, "une seconde exécution ne doit plus rien trouver à faire"


@pytest.mark.integration
def test_le_comptage_distingue_les_deux_cas(reparation, session):
    """C'est sur ces chiffres que se prend la décision d'exécuter, ou non."""
    _creer_orpheline(session, 5005, produit_existe=True)
    _creer_orpheline(session, 5006, produit_existe=False)

    total, rattachables, sans_produit = reparation.compter(session)

    assert (total, rattachables, sans_produit) == (2, 1, 1)


@pytest.mark.integration
def test_une_ligne_sans_idproduct_exploitable_reste_intacte(reparation, session):
    """
    Le script ne doit rien inventer.

    Une ligne dont le `raw_json` ne porte pas d'identifiant numérique est
    irréparable : elle doit rester telle quelle, et être signalée à la fin.
    """
    fichier = session.execute(text("""
        INSERT INTO cardmarket_import_files (file_type, file_url, status, started_at)
        VALUES ('test_reparation', 'test://x', 'success', now()) RETURNING id
    """)).scalar_one()
    ligne = session.execute(text("""
        INSERT INTO cardmarket_price_guide_entries
               (import_file_id, id_product, captured_at, raw_json)
        VALUES (:fid, NULL, now(), '{"avg": 1.0}'::jsonb)
        RETURNING id
    """), {"fid": fichier}).scalar_one()
    session.commit()

    reparation.creer_produits_manquants(session, dry_run=False)
    reparation.rattacher(session, taille_lot=1000)

    assert session.execute(
        text("SELECT id_product FROM cardmarket_price_guide_entries WHERE id = :i"),
        {"i": ligne}).scalar() is None
