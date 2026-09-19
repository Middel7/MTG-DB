"""
Non-régression sur le nettoyage des runs interrompus.

Le 19/09, un run de production est mort sans pouvoir écrire son statut : la base
était injoignable au moment précis où il tentait de se marquer `failed`. La ligne
est restée `running` indéfiniment. Trois dégâts : la supervision croit un import
en cours, `bulk_already_imported()` ne voit jamais de succès pour ce bulk, et
l'historique devient illisible.

Le nettoyage vivait dans `scripts/import_scryfall.py` et ne servait donc qu'à
Scryfall. Il est désormais dans `mtgdb.db.runs`, partagé par les quatre sources —
et testable par un import ordinaire, sans charger un script par son chemin.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from mtgdb.db.runs import marquer_runs_orphelins


@pytest.fixture
def base_locale(database_url):
    """
    Refuse de tourner ailleurs que sur une base locale.

    Ce test ÉCRIT dans `import_runs`. Lancé par mégarde avec la DATABASE_URL de
    production, il y insérerait des lignes de test.
    """
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test écrit en base : réservé à une base locale.")
    return database_url


@pytest.mark.integration
def test_un_run_interrompu_est_marque_failed(base_locale):
    engine = create_engine(base_locale)
    from mtgdb.db.engine import SessionLocal

    with engine.begin() as conn:
        orphelin = conn.execute(text("""
            INSERT INTO import_runs (source, source_file, started_at, status,
                                     cards_imported, printings_imported, errors_count)
            VALUES ('test-orphelin', 'test://verrou', now() - interval '9 hours', 'running',
                    0, 0, 0)
            RETURNING id
        """)).scalar()
        recent = conn.execute(text("""
            INSERT INTO import_runs (source, source_file, started_at, status,
                                     cards_imported, printings_imported, errors_count)
            VALUES ('test-orphelin', 'test://en-cours', now() - interval '10 minutes', 'running',
                    0, 0, 0)
            RETURNING id
        """)).scalar()

    try:
        with SessionLocal() as session:
            marques = marquer_runs_orphelins(session, source="test-orphelin",
                                                       older_than_hours=6)
        assert marques == 1, "seul le run trop ancien devait être marqué"

        with engine.connect() as conn:
            lignes = dict(conn.execute(text("""
                SELECT id, status FROM import_runs WHERE id = ANY(:ids)
            """), {"ids": [orphelin, recent]}).all())

        assert lignes[orphelin] == "failed"
        assert lignes[recent] == "running", (
            "un run démarré il y a 10 min peut très bien être vivant : "
            "le marquer casserait un import en cours"
        )

        with engine.connect() as conn:
            message = conn.execute(text(
                "SELECT error_message FROM import_runs WHERE id = :i"), {"i": orphelin}).scalar()
        assert "orphelin" in message.lower(), "le motif doit rester lisible dans l'historique"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM import_runs WHERE source = 'test-orphelin'"))
        engine.dispose()


@pytest.mark.integration
def test_un_run_reussi_n_est_jamais_touche(base_locale):
    engine = create_engine(base_locale)
    from mtgdb.db.engine import SessionLocal

    with engine.begin() as conn:
        reussi = conn.execute(text("""
            INSERT INTO import_runs (source, source_file, started_at, finished_at, status,
                                     cards_imported, printings_imported, errors_count)
            VALUES ('test-orphelin', 'test://ok', now() - interval '3 days',
                    now() - interval '3 days', 'success', 0, 0, 0)
            RETURNING id
        """)).scalar()
    try:
        with SessionLocal() as session:
            marquer_runs_orphelins(session, source="test-orphelin", older_than_hours=6)
        with engine.connect() as conn:
            statut = conn.execute(text(
                "SELECT status FROM import_runs WHERE id = :i"), {"i": reussi}).scalar()
        assert statut == "success"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM import_runs WHERE source = 'test-orphelin'"))
        engine.dispose()
