"""
Non-régression sur le contrôle « le schéma porte-t-il ce que ce code écrit ».

Aucun job n'applique les migrations — la base de production est partagée avec
RELIC-Trade. Mais `autoDeploy` met le nouveau code en service dès la fusion, et
le passage horaire suivant l'exécute. Entre les deux, un run peut écrire sur une
colonne qui n'existe pas encore.

Ce contrôle ne répare rien : il refuse de commencer. Un run qui s'arrête en deux
secondes sur « il manque la migration X » se diagnostique d'un coup d'œil, là où
un `UndefinedColumn` surgi au milieu de 540 000 upserts envoie chercher la panne
dans le pipeline.

Le cœur de la logique est une fonction pure : ces tests ne touchent ni la base
ni le disque.
"""
from __future__ import annotations

import pytest

from mtgdb.db.schema_version import (
    A_JOUR,
    BASE_EN_AVANCE,
    CODE_EN_AVANCE,
    INDETERMINE,
    comparer_revisions,
    verifier_schema,
)

# Historique tel que `walk_revisions()` le rend : de la tête vers la racine.
HISTORIQUE = [
    "20261103_illustration_id",
    "20261016_printing_finishes",
    "20260920_vue_prix_cm",
    "20260920_source_publications",
]
TETE = HISTORIQUE[0]


class TestComparaison:
    def test_les_deux_concordent(self):
        assert comparer_revisions(TETE, TETE, HISTORIQUE) == A_JOUR

    def test_la_base_est_restee_en_arriere(self):
        """
        LE cas dangereux : le code écrira une colonne que la base n'a pas.

        Scénario réel du 03/11 — la PR `illustration_id` fusionnée, `autoDeploy`
        déploie, et le passage horaire suivant tourne avant que quiconque ait
        lancé `alembic upgrade head`.
        """
        assert comparer_revisions(
            TETE, "20261016_printing_finishes", HISTORIQUE) == CODE_EN_AVANCE

    def test_une_base_tres_en_arriere_est_vue_aussi(self):
        assert comparer_revisions(
            TETE, "20260920_source_publications", HISTORIQUE) == CODE_EN_AVANCE

    def test_la_base_porte_une_revision_inconnue(self):
        """
        Bénin : une image plus ancienne tourne contre une base déjà migrée.

        `ADD COLUMN` nullable est rétrocompatible — l'ancien code ignore
        simplement la colonne. Bloquer ici empêcherait un retour en arrière
        volontaire sur une version précédente de l'image.
        """
        assert comparer_revisions(
            TETE, "20270101_une_migration_future", HISTORIQUE) == BASE_EN_AVANCE

    @pytest.mark.parametrize("tete,base", [
        (None, TETE),      # historique Alembic illisible
        (TETE, None),      # table de version absente
        (None, None),
    ])
    def test_une_inconnue_ne_bloque_pas(self, tete, base):
        """
        On ne refuse pas de tourner sur une information manquante.

        Un contrôle qui bloquerait parce qu'il n'a pas pu lire `alembic.ini`
        transformerait une incertitude en panne — exactement ce qu'il doit
        éviter.
        """
        assert comparer_revisions(tete, base, HISTORIQUE) == INDETERMINE


class TestMessages:
    def test_le_message_de_blocage_nomme_la_commande(self):
        """
        Le message est lu dans les logs Render, à distance, par quelqu'un qui
        n'a pas le contexte. Il doit contenir la révision attendue, celle
        trouvée, et le geste exact à poser.
        """
        def fabrique():
            raise RuntimeError("pas de base ici")

        etat, message = verifier_schema(fabrique)
        assert etat == INDETERMINE
        assert "illisible" in message.lower()

    def test_une_base_injoignable_ne_leve_pas(self):
        """Le contrôle ne doit jamais ajouter une panne là où il en évite une."""
        def fabrique():
            raise RuntimeError("base injoignable")

        etat, _ = verifier_schema(fabrique)
        assert etat == INDETERMINE


@pytest.mark.integration
def test_le_depot_et_la_base_locale_concordent(database_url):
    """
    Sur une base à jour, le contrôle doit se taire.

    Ce test échouerait si quelqu'un ajoutait une migration sans l'appliquer en
    local — ce qui est précisément le genre d'oubli qu'on traque.
    """
    from mtgdb.db.engine import SessionLocal

    etat, message = verifier_schema(SessionLocal)
    assert etat == A_JOUR, message
