"""
Non-régression sur la reprise après interruption transitoire de la base.

Deux runs de production ont été perdus faute de cette reprise :
le 17/09 (`SSL connection has been closed unexpectedly`, 78 min) et le 19/09
(`the database system is not yet accepting connections`, 70 min). Dans les deux
cas l'interruption a duré moins longtemps que le travail jeté.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from mtgdb.db.retry import is_transient_error, retry_transient


def _erreur(message: str, classe=OperationalError) -> Exception:
    return classe(message, None, Exception(message))


class TestDetection:
    @pytest.mark.parametrize("message", [
        # Le message exact du run Render perdu le 19/09.
        'FATAL: the database system is not yet accepting connections\n'
        'DETAIL: Consistent recovery state has not been yet reached.',
        # Celui du run lancé depuis le poste, perdu le 17/09.
        "SSL connection has been closed unexpectedly",
        "server closed the connection unexpectedly",
        "FATAL: the database system is starting up",
        "could not connect to server: Connection refused",
        "FATAL: terminating connection due to administrator command",
        "FATAL: sorry, too many clients already",
    ])
    def test_les_coupures_reelles_sont_reconnues(self, message):
        assert is_transient_error(_erreur(message)) is True

    def test_la_casse_n_a_pas_d_importance(self):
        assert is_transient_error(_erreur("SSL CONNECTION HAS BEEN CLOSED UNEXPECTEDLY")) is True

    @pytest.mark.parametrize("exc", [
        _erreur('duplicate key value violates unique constraint', IntegrityError),
        _erreur('column "price_date" does not exist', ProgrammingError),
        ValueError("rien à voir avec la base"),
    ])
    def test_une_vraie_erreur_n_est_pas_transitoire(self, exc):
        # La rejouer masquerait un bug et ferait tourner le run pour rien.
        assert is_transient_error(exc) is False

    def test_une_operationalerror_de_logique_n_est_pas_transitoire(self):
        assert is_transient_error(_erreur("deadlock detected")) is False


class TestRejeu:
    def test_reussit_apres_deux_coupures(self):
        tentatives = []
        dodos = []

        def operation():
            tentatives.append(1)
            if len(tentatives) < 3:
                raise _erreur("server closed the connection unexpectedly")
            return "importé"

        resultat = retry_transient(operation, description="test",
                                   delays=(1, 2, 3), sleep=dodos.append)
        assert resultat == "importé"
        assert len(tentatives) == 3
        assert dodos == [1, 2]  # a bien attendu entre les tentatives

    def test_n_insiste_pas_sur_une_vraie_erreur(self):
        tentatives = []

        def operation():
            tentatives.append(1)
            raise _erreur("null value in column violates not-null", IntegrityError)

        with pytest.raises(IntegrityError):
            retry_transient(operation, description="test", delays=(1, 2), sleep=lambda _: None)
        assert len(tentatives) == 1, "une erreur de données ne doit jamais être rejouée"

    def test_abandonne_apres_le_dernier_delai(self):
        tentatives = []

        def operation():
            tentatives.append(1)
            raise _erreur("the database system is starting up")

        with pytest.raises(OperationalError):
            retry_transient(operation, description="test", delays=(1, 2), sleep=lambda _: None)
        assert len(tentatives) == 3, "une tentative initiale + une par délai"

    def test_le_nettoyage_est_appele_avant_chaque_reprise(self):
        nettoyages = []
        tentatives = []

        def operation():
            tentatives.append(1)
            if len(tentatives) < 3:
                raise _erreur("connection refused")
            return True

        retry_transient(operation, description="test",
                        on_retry=lambda: nettoyages.append(1),
                        delays=(1, 2, 3), sleep=lambda _: None)
        assert len(nettoyages) == 2

    def test_un_nettoyage_qui_echoue_ne_masque_pas_l_erreur(self):
        """
        Le piège qui a tué le run du 19/09 : `session.rollback()`, appelé dans le
        bloc de rattrapage, a levé à son tour sur la base injoignable — et c'est
        cette erreur de nettoyage qui est remontée, hors de tout `except`.
        """
        tentatives = []

        def operation():
            tentatives.append(1)
            if len(tentatives) < 2:
                raise _erreur("could not connect to server")
            return "ok"

        def nettoyage_cassé():
            raise _erreur("could not connect to server")

        assert retry_transient(operation, description="test",
                               on_retry=nettoyage_cassé,
                               delays=(1,), sleep=lambda _: None) == "ok"

    def test_le_cas_nominal_n_attend_jamais(self):
        dodos = []
        assert retry_transient(lambda: 42, description="test", sleep=dodos.append) == 42
        assert dodos == []
