"""
Non-régression sur le suivi de fraîcheur des sources.

Ce suivi répond à « quand la source a-t-elle publié, et quand l'avons-nous
absorbé ». Deux propriétés comptent plus que les autres :

  - il ne doit JAMAIS faire échouer un import. Une ligne de traçabilité perdue
    est sans gravité ; un import de 540 000 impressions perdu ne l'est pas ;
  - il doit rester idempotent sous un pipeline qui passe toutes les heures,
    alors que les sources ne publient qu'une ou deux fois par jour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from mtgdb.db.publications import (
    SOURCES_PAR_FILE_TYPE,
    enregistrer_publication,
    marquer_publication_importee,
    parser_date_http,
)

SOURCE_TEST = "test-publications"


class TestParsingDateHttp:
    def test_le_format_de_cardmarket_est_compris(self):
        # Valeur relevée telle quelle dans cardmarket_import_files le 20/09.
        resultat = parser_date_http("Sun, 20 Sep 2026 00:42:36 GMT")
        assert resultat == datetime(2026, 9, 20, 0, 42, 36, tzinfo=timezone.utc)

    def test_le_resultat_porte_un_fuseau(self):
        # Sans fuseau, la soustraction avec now(tz) lèverait — et le calcul du
        # retard est précisément ce à quoi sert cette date.
        assert parser_date_http("Sun, 20 Sep 2026 00:42:36 GMT").tzinfo is not None

    @pytest.mark.parametrize("valeur", [None, "", "pas une date", "20/09/2026"])
    def test_une_valeur_illisible_ne_leve_pas(self, valeur):
        # Une publication sans date reste plus utile qu'une publication non
        # enregistrée : on renvoie None, on ne casse rien.
        assert parser_date_http(valeur) is None


class TestCorrespondanceDesSources:
    def test_les_deux_exports_cardmarket_sont_couverts(self):
        assert set(SOURCES_PAR_FILE_TYPE) == {
            "price_guide_magic", "product_catalog_magic_singles"}

    def test_un_export_inconnu_ne_produit_pas_de_source_fantome(self):
        # Le code de download.py fait `.get(file_type)` puis teste : un nouvel
        # export Cardmarket sera simplement non suivi, jamais mal suivi.
        assert SOURCES_PAR_FILE_TYPE.get("un_nouvel_export_2027") is None


class TestRobustesse:
    def test_une_base_injoignable_ne_leve_pas(self):
        """
        Le point le plus important du module.

        Si cette garantie tombe, une base momentanément indisponible ne fait
        plus perdre une ligne de suivi : elle fait perdre l'import entier.
        """
        def fabrique_cassee():
            raise RuntimeError("base injoignable")

        assert enregistrer_publication(
            SOURCE_TEST, "v1", session_factory=fabrique_cassee) is False
        assert marquer_publication_importee(
            SOURCE_TEST, "v1", session_factory=fabrique_cassee) is False

    @pytest.mark.parametrize("version", ["", None])
    def test_une_version_vide_est_ignoree(self, version):
        # Un HEAD sans ETag ne doit pas créer de ligne à clé vide, qui
        # collisionnerait avec la suivante.
        assert enregistrer_publication(SOURCE_TEST, version) is False
        assert marquer_publication_importee(SOURCE_TEST, version) is False


@pytest.fixture
def base_locale(database_url):
    """Ces tests écrivent : refus de tourner ailleurs que sur une base locale."""
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test écrit en base : réservé à une base locale.")
    return database_url


@pytest.fixture
def nettoyer(base_locale):
    engine = create_engine(base_locale)
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM mtgdb_source_publications WHERE source = :s"),
                     {"s": SOURCE_TEST})
    engine.dispose()


@pytest.mark.integration
class TestEcriture:
    def test_une_nouvelle_version_est_signalee_une_seule_fois(self, nettoyer):
        publiee = datetime.now(timezone.utc) - timedelta(hours=2)

        assert enregistrer_publication(SOURCE_TEST, "bulk-A", publiee) is True, (
            "la première détection doit être signalée comme nouvelle")
        assert enregistrer_publication(SOURCE_TEST, "bulk-A", publiee) is False, (
            "un passage horaire qui revoit la même version ne doit rien signaler")

        with nettoyer.connect() as conn:
            lignes = conn.execute(text(
                "SELECT count(*) FROM mtgdb_source_publications WHERE source = :s"),
                {"s": SOURCE_TEST}).scalar()
        assert lignes == 1, "24 passages par jour ne doivent pas produire 24 lignes"

    def test_revoir_une_version_connue_rafraichit_la_veille(self, nettoyer):
        """
        `last_seen_at` est ce qui distingue « la source est calme » de « le cron
        ne tourne plus ». Sans mise à jour, les deux cas seraient identiques.
        """
        enregistrer_publication(SOURCE_TEST, "bulk-A")
        with nettoyer.begin() as conn:
            conn.execute(text(
                "UPDATE mtgdb_source_publications "
                "SET last_seen_at = now() - interval '5 hours' WHERE source = :s"),
                {"s": SOURCE_TEST})

        enregistrer_publication(SOURCE_TEST, "bulk-A")

        with nettoyer.connect() as conn:
            age = conn.execute(text(
                "SELECT now() - last_seen_at FROM mtgdb_source_publications "
                "WHERE source = :s"), {"s": SOURCE_TEST}).scalar()
        assert age < timedelta(minutes=1), "la date de dernière vérification n'a pas avancé"

    def test_deux_versions_coexistent(self, nettoyer):
        enregistrer_publication(SOURCE_TEST, "bulk-A")
        enregistrer_publication(SOURCE_TEST, "bulk-B")
        with nettoyer.connect() as conn:
            lignes = conn.execute(text(
                "SELECT count(*) FROM mtgdb_source_publications WHERE source = :s"),
                {"s": SOURCE_TEST}).scalar()
        assert lignes == 2, "l'historique des publications doit être conservé"

    def test_l_absorption_est_datee_une_fois_pour_toutes(self, nettoyer):
        """
        Un réimport forcé ne doit pas réécrire la date de première absorption :
        c'est elle qui mesure la réactivité du pipeline.
        """
        enregistrer_publication(SOURCE_TEST, "bulk-A")
        assert marquer_publication_importee(SOURCE_TEST, "bulk-A") is True

        with nettoyer.connect() as conn:
            premiere = conn.execute(text(
                "SELECT imported_at FROM mtgdb_source_publications WHERE source = :s"),
                {"s": SOURCE_TEST}).scalar()

        assert marquer_publication_importee(SOURCE_TEST, "bulk-A") is False, (
            "une version déjà absorbée ne doit pas être remarquée")

        with nettoyer.connect() as conn:
            apres = conn.execute(text(
                "SELECT imported_at FROM mtgdb_source_publications WHERE source = :s"),
                {"s": SOURCE_TEST}).scalar()
        assert premiere == apres

    def test_une_version_jamais_vue_ne_peut_pas_etre_absorbee(self, nettoyer):
        assert marquer_publication_importee(SOURCE_TEST, "bulk-inexistant") is False


@pytest.mark.integration
def test_la_vue_expose_les_colonnes_attendues(base_locale):
    """
    La vue est le contrat public du suivi : `scripts/fraicheur.py` lit ces noms,
    et RELIC-Trade pourrait les lire aussi.
    """
    engine = create_engine(base_locale)
    try:
        with engine.connect() as conn:
            colonnes = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'mtgdb_fraicheur_sources'"))}
    finally:
        engine.dispose()

    assert {"source", "verifiee_le", "publiee_le", "dernier_import_le", "a_jour",
            "retard", "depuis_derniere_verification",
            "depuis_dernier_import"} <= colonnes


# ── Absorption côté Cardmarket ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def import_cardmarket_all():
    import importlib.util
    from pathlib import Path

    racine = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "import_cardmarket_all_sous_test", racine / "scripts" / "import_cardmarket_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FausseLigneImport:
    """Le strict nécessaire de CardmarketImportFile pour `tracer_absorption`."""

    def __init__(self, status="success", etag='"abc123"',
                 file_type="price_guide_magic"):
        self.status = status
        self.etag = etag
        self.file_type = file_type


class TestAbsorptionCardmarket:
    """
    `tracer_absorption()` couvre le cas qui manquait : un import Cardmarket qui
    vient de RÉUSSIR.

    `download_file()` ne traitait que le cas « ETag identique », si bien qu'une
    version fraîchement importée restait affichée « EN ATTENTE » et aurait
    déclenché une fausse alerte six heures plus tard. Constaté sur le run du
    20/09 : le catalogue produits, importé avec succès à 15:28, apparaissait en
    retard de 3 h 26 dans `mtgdb_fraicheur_sources`.
    """

    def test_un_import_reussi_est_trace(self, import_cardmarket_all, monkeypatch):
        appels = []
        monkeypatch.setattr(import_cardmarket_all, "marquer_publication_importee",
                            lambda source, version: appels.append((source, version)))

        import_cardmarket_all.tracer_absorption(
            FausseLigneImport(file_type="product_catalog_magic_singles", etag='"81d495"'))

        assert appels == [("cardmarket_product_catalog", '"81d495"')]

    def test_rien_n_est_trace_sans_ligne_d_import(self, import_cardmarket_all):
        assert import_cardmarket_all.tracer_absorption(None) is None

    @pytest.mark.parametrize("ligne,motif", [
        (FausseLigneImport(status="failed"), "un import en échec n'absorbe rien"),
        (FausseLigneImport(status="skipped_not_modified"),
         "le cas ETag identique est déjà traité par download_file()"),
        (FausseLigneImport(etag=None), "sans ETag, aucune version à identifier"),
        (FausseLigneImport(file_type="un_export_inconnu_2027"),
         "un export non suivi ne doit pas créer de source fantôme"),
    ])
    def test_les_cas_sans_absorption(self, import_cardmarket_all, monkeypatch, ligne, motif):
        appels = []
        monkeypatch.setattr(import_cardmarket_all, "marquer_publication_importee",
                            lambda source, version: appels.append((source, version)))

        import_cardmarket_all.tracer_absorption(ligne)

        assert appels == [], motif
