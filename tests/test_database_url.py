"""
Non-régression sur le traitement de DATABASE_URL.

Ces règles vivaient dans `update-prod.ps1`. Elles sont passées en Python parce
que la production tourne désormais dans un Cron Job Render, où aucun script
PowerShell n'est sur le chemin. Si l'une d'elles disparaît, le symptôme est un
échec au premier run en conteneur — ou pire, un run vert sur la mauvaise base.
"""
from __future__ import annotations

import pytest

from mtgdb.db.urls import (
    database_host,
    is_local_database_url,
    normalize_database_url,
    redact_database_url,
)

RENDER_LEGACY = "postgres://relictrade_user:s3cr3t@dpg-abc.frankfurt-postgres.render.com/relictrade"
RENDER_MODERN = "postgresql://relictrade_user:s3cr3t@dpg-abc.frankfurt-postgres.render.com/relictrade"


class TestNormalisation:
    def test_le_prefixe_render_est_corrige(self):
        # Render affiche encore postgres://, que SQLAlchemy 2 refuse.
        assert normalize_database_url(RENDER_LEGACY) == RENDER_MODERN

    def test_une_url_deja_correcte_est_inchangee(self):
        assert normalize_database_url(RENDER_MODERN) == RENDER_MODERN

    def test_la_normalisation_est_idempotente(self):
        une_fois = normalize_database_url(RENDER_LEGACY)
        assert normalize_database_url(une_fois) == une_fois

    def test_le_pilote_explicite_est_preserve(self):
        url = "postgresql+psycopg2://u:p@host/db"
        assert normalize_database_url(url) == url

    @pytest.mark.parametrize("valeur", [None, ""])
    def test_une_valeur_vide_traverse_sans_erreur(self, valeur):
        # L'absence de DATABASE_URL est diagnostiquée ailleurs, avec un message
        # utile : cette fonction ne doit pas la transformer en exception opaque.
        assert normalize_database_url(valeur) == valeur

    def test_le_reste_de_l_url_est_intact(self):
        url = "postgres://u:p@host:5432/db?sslmode=require&application_name=mtgdb"
        assert normalize_database_url(url) == (
            "postgresql://u:p@host:5432/db?sslmode=require&application_name=mtgdb"
        )

    def test_seul_le_prefixe_est_remplace(self):
        # Un mot de passe qui contient la chaîne « postgres:// » ne doit pas
        # être réécrit : la substitution est ancrée en début d'URL.
        url = "postgresql://u:postgres%3A%2F%2Fx@host/db"
        assert normalize_database_url(url) == url


class TestDetectionLocale:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://postgres:pass@localhost:5432/manamind",
            "postgresql://postgres:pass@127.0.0.1:5432/manamind",
            "postgresql://postgres:pass@localhost/manamind",
            "postgresql://postgres:pass@[::1]:5432/manamind",
            "postgresql://postgres:pass@0.0.0.0:5432/manamind",
            "postgres://postgres:pass@LOCALHOST:5432/manamind",
        ],
    )
    def test_une_base_locale_est_reconnue(self, url):
        assert is_local_database_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            RENDER_MODERN,
            RENDER_LEGACY,
            "postgresql://u:p@db.internal:5432/x",
            "postgresql://u:p@postgres:5432/manamind",  # service docker-compose
        ],
    )
    def test_une_base_distante_n_est_pas_reconnue_comme_locale(self, url):
        assert is_local_database_url(url) is False

    def test_un_identifiant_nomme_localhost_ne_trompe_pas(self):
        # Le piège classique d'une détection par « localhost in url ».
        assert is_local_database_url("postgresql://localhost:pw@render.com/db") is False

    def test_une_url_illisible_ne_leve_pas(self):
        assert database_host("n'importe quoi") == ""
        assert is_local_database_url("n'importe quoi") is False
        assert is_local_database_url(None) is False


class TestMasquage:
    def test_le_mot_de_passe_n_apparait_pas(self):
        # Les journaux d'un Cron Job Render sont lisibles dans le dashboard.
        masque = redact_database_url(RENDER_MODERN)
        assert "s3cr3t" not in masque
        assert "relictrade_user" in masque
        assert "frankfurt-postgres.render.com" in masque

    def test_une_url_absente_est_annoncee(self):
        assert redact_database_url(None) == "(absent)"
